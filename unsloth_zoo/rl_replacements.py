# Unsloth Zoo - Utilities for Unsloth
# Copyright 2023-present Daniel Han-Chen, Michael Han-Chen & the Unsloth team. All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

__all__ = [
    "RL_REPLACEMENTS"
]

import torch
import inspect
import os
import numpy as np
from typing import Union, Callable, Optional, List, Dict

RL_REPLACEMENTS = dict()

torch_compile_options = {
    "epilogue_fusion"   : True,
    "max_autotune"      : False, # Disable Triton mm kernels
    "shape_padding"     : True,
    "trace.enabled"     : False,
    "triton.cudagraphs" : False,
}

# https://github.com/huggingface/trl/blob/main/trl/trainer/utils.py#L1674
@torch.compile(dynamic = True, fullgraph = True, options = torch_compile_options,)
def selective_log_softmax(logits, index):
    logits = logits.to(torch.float32)
    selected_logits = torch.gather(logits, dim = -1, index = index.unsqueeze(-1)).squeeze(-1)
    # loop to reduce peak mem consumption
    # logsumexp_values = torch.stack([torch.logsumexp(lg, dim=-1) for lg in logits])
    logsumexp_values = torch.logsumexp(logits, dim = -1)
    per_token_logps = selected_logits - logsumexp_values  # log_softmax(x_i) = x_i - logsumexp(x)
    return per_token_logps
pass
RL_REPLACEMENTS["selective_log_softmax"] = selective_log_softmax


# Custom compiled GRPO loss - creates 3 Triton kernels
def grpo_compute_loss(old_logits, new_logits, input_ids, mask, beta, advantages):
    # All Unsloth Zoo code licensed under LGPLv3
    old_logits = old_logits.to(torch.float32)
    new_logits = new_logits.to(torch.float32)
    input_ids  = input_ids.unsqueeze(-1)

    # x_i - logsumexp(x_i)
    old_x = torch.gather(old_logits, dim = -1, index = input_ids).squeeze(-1)
    new_x = torch.gather(new_logits, dim = -1, index = input_ids).squeeze(-1)
    old = old_x - torch.logsumexp(old_logits, dim = -1)
    new = new_x - torch.logsumexp(new_logits, dim = -1)

    # Reverse KL
    kl_i = torch.exp(old - new) - (old - new) - 1.0
    # Full correct reverse KL divergence?? Missing term maybe?
    # kl_i = torch.exp(new) * kl_i

    # Below is forward KL (normal KL)
    # kl_i = torch.exp(old) * (old - new)

    # Must detach - otherwise gradients are not propagated correctly!
    # exp(x - x) == 1
    loss_i = torch.exp(new - new.detach()) * advantages.unsqueeze(1)
    loss_i = -(loss_i - beta * kl_i)

    mask = mask.to(torch.float32)
    n_mask_per_reward = mask.sum(1)

    # See https://github.com/huggingface/trl/pull/2881
    # loss_per_reward = (loss_i * mask).sum(1) / n_mask_per_reward
    # loss = loss_per_reward.mean()
    loss = (loss_i * mask).sum() / mask.sum()
    
    # Get metrics as well which are folded
    with torch.inference_mode():
        completion_length = n_mask_per_reward.mean()
        mean_kl_per_reward = (kl_i * mask).sum(1) / n_mask_per_reward
        mean_kl = mean_kl_per_reward.mean()
    pass
    return loss, completion_length, mean_kl
pass
# grpo_compute_loss = torch.compile(_grpo_compute_loss,
#     dynamic = True, fullgraph = True, options = torch_compile_options,
# )
RL_REPLACEMENTS["grpo_compute_loss"] = grpo_compute_loss


# Unsloth's memory efficient GRPO implementation
class UnslothEfficientGRPO(torch.autograd.Function):
    # All Unsloth Zoo code licensed under LGPLv3
    @staticmethod
    def forward(ctx, _new_hidden_states, _old_hidden_states, lm_head, _input_ids, _mask, _advantages, beta, scaler = None, n_chunks = 1):
        def compute_loss(new_hidden_states, old_hidden_states, input_ids, mask, advantages, scaling):
            new_logits = torch.matmul(new_hidden_states, lm_head.t())
            new_logits = new_logits[:, :-1, :] # exclude the last logit: it corresponds to the next token pred
            old_logits = torch.matmul(old_hidden_states, lm_head.t())
            old_logits = old_logits[:, :-1, :] # exclude the last logit: it corresponds to the next token pred
            loss, completion_length, mean_kl = grpo_compute_loss(
                old_logits, new_logits, input_ids, mask, beta, advantages,
            )
            # Scale loss if needed for mixed precision training
            scaled_loss = loss * scaling
            # Must add .loss.detach otherwise autograd uses 2x VRAM
            return scaled_loss, (loss.detach(), completion_length, mean_kl,)
        pass

        device =_new_hidden_states.device
        grad_inputs = torch.empty_like(_new_hidden_states)
        accumulated_loss              = torch.zeros(1, device = device)
        accumulated_completion_length = torch.zeros(1, device = device)
        accumulated_mean_kl           = torch.zeros(1, device = device)

        def accumulate_chunk(new_hidden_states_j, old_hidden_states_j, input_ids_j, mask_j, advantages_j, scaling):
            (chunk_grad_input,), (chunk_loss, (unscaled_loss, chunk_completion_length, chunk_mean_kl,)) = torch.func.grad_and_value(
                compute_loss,
                argnums = (0,),
                has_aux = True,
            )(new_hidden_states_j, old_hidden_states_j, input_ids_j, mask_j, advantages_j, scaling)
            accumulated_loss             .add_(unscaled_loss)
            accumulated_completion_length.add_(chunk_completion_length)
            accumulated_mean_kl          .add_(chunk_mean_kl)
            return chunk_grad_input
        pass

        accumulate_chunk = torch.compile(
            accumulate_chunk,
            fullgraph = True,
            options = torch_compile_options,
        )

        grad_inputs_chunks = torch.chunk(grad_inputs,        chunks = n_chunks, dim = 0)
        new_hidden_states  = torch.chunk(_new_hidden_states, chunks = n_chunks, dim = 0)
        old_hidden_states  = torch.chunk(_old_hidden_states, chunks = n_chunks, dim = 0)
        input_ids          = torch.chunk(_input_ids,         chunks = n_chunks, dim = 0)
        mask               = torch.chunk(_mask,              chunks = n_chunks, dim = 0)
        advantages         = torch.chunk(_advantages,        chunks = n_chunks, dim = 0)

        # Get mixed precision scaling if seen
        scaling = scaler.get_scale() if scaler is not None else 1.0

        # Force torch.compile to use dynamic shapes for seqlen dim
        mark_dynamic = lambda x: torch._dynamo.mark_dynamic(x, 1)

        for (grad_inputs_j, new_hidden_states_j, old_hidden_states_j, input_ids_j, mask_j, advantages_j,) in \
            zip(grad_inputs_chunks, new_hidden_states, old_hidden_states, input_ids, mask, advantages):

            mark_dynamic(new_hidden_states_j)
            mark_dynamic(old_hidden_states_j)
            mark_dynamic(input_ids_j)
            mark_dynamic(mask_j)

            grad_inputs_j.copy_(
                accumulate_chunk(new_hidden_states_j, old_hidden_states_j, input_ids_j, mask_j, advantages_j, scaling)
            )
        pass

        grad_inputs                  .div_(n_chunks)
        accumulated_loss             .div_(n_chunks)
        accumulated_completion_length.div_(n_chunks)
        accumulated_mean_kl          .div_(n_chunks)
        ctx.save_for_backward(grad_inputs)

        return (
            accumulated_loss,
            accumulated_completion_length,
            accumulated_mean_kl,
        )
    pass

    @staticmethod
    def backward(ctx, grad_output, dcompletion_length, dmean_kl):
        (grad_input,) = ctx.saved_tensors
        return (grad_input, None, None, None, None, None, None, None, None,)
    pass
pass
RL_REPLACEMENTS["UnslothEfficientGRPO"] = UnslothEfficientGRPO


def grpo_accumulated_loss(
    trainer,
    input_ids,
    logits_to_keep,
    completion_mask,
    advantages,
    n_chunks = -1,
):
    # All Unsloth Zoo code licensed under LGPLv3
    bsz, qlen = input_ids.shape
    # Find closest multiple
    factors = [i for i in range(1, bsz + 1) if bsz % i == 0]
    if n_chunks == -1: n_chunks = bsz
    n_chunks = factors[min(np.searchsorted(factors, n_chunks), len(factors)-1)]

    mixed_dtype = torch.float16 if os.environ.get('ACCELERATE_MIXED_PRECISION', 'fp16') == 'fp16' else torch.bfloat16
    os.environ["UNSLOTH_RETURN_HIDDEN_STATES"] = "1"

    completion_input_ids = input_ids[:, -logits_to_keep:]
    lm_head = trainer.model.get_output_embeddings().weight

    with torch.amp.autocast(device_type = "cuda", dtype = mixed_dtype):
        with torch.inference_mode(), trainer.accelerator.unwrap_model(trainer.model, keep_fp32_wrapper = False).disable_adapter():
            old_hidden_states = trainer.model(input_ids = input_ids, logits_to_keep = logits_to_keep + 1).logits
        pass

        new_hidden_states = trainer.model(input_ids = input_ids, logits_to_keep = logits_to_keep + 1).logits
        
        loss, completion_length, mean_kl = UnslothEfficientGRPO.apply(
            new_hidden_states, old_hidden_states, lm_head,
            completion_input_ids, completion_mask, advantages, trainer.beta,
            trainer.accelerator.scaler,
            n_chunks, 
        )
        return loss, completion_length, mean_kl

        # Old non efficient code path
        new_logits = torch.matmul(new_hidden_states, lm_head.t())
        new_logits = new_logits[:, :-1, :] # exclude the last logit: it corresponds to the next token pred
        old_logits = torch.matmul(old_hidden_states, lm_head.t())
        old_logits = old_logits[:, :-1, :] # exclude the last logit: it corresponds to the next token pred
        loss, completion_length, mean_kl = grpo_compute_loss(
            old_logits, new_logits, completion_input_ids, completion_mask, trainer.beta, advantages,
        )
        return loss, completion_length, mean_kl
    pass
pass
RL_REPLACEMENTS["grpo_accumulated_loss"] = grpo_accumulated_loss


from datasets import (Dataset, IterableDataset,)
from trl.trainer.utils import ConstantLengthDataset
# Faster SFTTrainer prepare_dataset
def sft_prepare_dataset(
    self,
    dataset: Union[Dataset, IterableDataset],
    processing_class,
    args,
    packing: bool,
    formatting_func: Optional[Callable[[dict], str]],
    dataset_name: str,
) -> Union[Dataset, IterableDataset]:
    # All Unsloth Zoo code licensed under LGPLv3
    if isinstance(dataset, ConstantLengthDataset): return dataset

    map_kwargs = {}
    use_desc = isinstance(dataset, Dataset)

    # Get max length
    max_seq_length = getattr(args, "max_length", 0)
    if max_seq_length == 0: max_seq_length = getattr(args, "max_seq_length", 0)
    if max_seq_length == 0: max_seq_length = getattr(self, "max_seq_length", 0)
    if max_seq_length == 0: max_seq_length = getattr(self, "max_seq", 0)
    dataset_text_field = getattr(args, "dataset_text_field", "text")
    do_truncation = max_seq_length != 0
    do_formatting_func = False

    # Check if already tokenized so skip
    from transformers import DataCollatorForSeq2Seq
    column_names = set(next(iter(dataset)).keys())
    if "input_ids" in column_names:
        # Most likely forgot data collator!
        from transformers import DataCollatorForSeq2Seq
        self.data_collator = DataCollatorForSeq2Seq(processing_class)
        return dataset
    elif dataset_text_field not in column_names:
        do_formatting_func = True
        if formatting_func is None:
            raise RuntimeError("Unsloth: You must specify a `formatting_func`")
    pass

    # Check double BOS tokens
    if do_formatting_func:
        test_text = formatting_func(dataset[0])
        if not isinstance(test_text, list):
            raise ValueError(
                "Unsloth: The `formatting_func` should return a list of processed strings."
            )
        test_text = test_text[0]
    else:
        test_text = dataset[0][dataset_text_field]
    chat_template = getattr(processing_class, 'chat_template', None)
    chat_template = '' if chat_template is None else chat_template
    add_special_tokens = True

    if getattr(processing_class, 'bos_token', None) is not None:
        if test_text.startswith(processing_class.bos_token) or processing_class.bos_token in chat_template:
            add_special_tokens = False
            print("Unsloth: We found double BOS tokens - we shall remove one automatically.")
    pass

    # Create tokenize function
    def _tokenize(example):
        return processing_class(
            example[dataset_text_field] if not do_formatting_func else formatting_func(example),
            truncation = do_truncation,
            max_length = max_seq_length,
            return_token_type_ids = False,
            add_special_tokens = add_special_tokens,
        )
    pass

    map_kwargs["num_proc"] = getattr(args, "dataset_num_proc", 2)
    if use_desc: map_kwargs["desc"] = f'Tokenizing to ["{dataset_text_field}"]'
    dataset = dataset.map(_tokenize, batched = True, **map_kwargs)

    if packing:
        if max_seq_length == 0:
            raise ValueError("When packing is enabled, `max_seq_length` can't be `None`.")

        if use_desc: map_kwargs["desc"] = f"Packing {dataset_name} dataset"
        dataset = dataset.select_columns("input_ids").map(
            pack_examples,
            batched = True,
            fn_kwargs = {"seq_length": max_seq_length,},
            **map_kwargs,
        )
    return dataset
pass
RL_REPLACEMENTS["sft_prepare_dataset"] = sft_prepare_dataset

# Unsloth Zoo - Utilities for Unsloth
# Copyright 2023-present Daniel Han-Chen, Michael Han-Chen & the Unsloth team. All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
