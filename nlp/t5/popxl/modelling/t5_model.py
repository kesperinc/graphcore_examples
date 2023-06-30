# Copyright (c) 2023 Graphcore Ltd. All rights reserved.
import numpy as np
from typing import Dict
from config import T5Config
import torch

import popxl

import popxl_addons as addons
from popxl_addons import NamedTensors
from popxl_addons.named_tensors import NamedTensorData

from .embedding import T5EmbeddingsTP, T5DecoderEmbeddingsTP
from .encoder import T5EncoderTP, T5EncoderHead, T5EncoderBlockTP
from .decoder import T5DecoderTP, T5DecoderBlockTP
from .layer_norm import T5LayerNorm

from transformers.models.t5.modeling_t5 import T5Model as HFModel


class T5ModelTP(addons.Module):
    def __init__(self, config: T5Config, include_layer_norm=True):
        super().__init__()
        self.config = config
        # sharded, then last bit identical
        self.dec_embeddings = T5DecoderEmbeddingsTP(self.config)
        # identical inputs, then sharded, then identical
        self.encoder = T5EncoderTP(self.config)
        self.encoder_head = T5EncoderHead(self.config)
        self.decoder = T5DecoderTP(self.config)
        # identical
        self.include_layer_norm = include_layer_norm
        if self.include_layer_norm:
            self.ln_f = T5LayerNorm(self.config)

    def build(
        self,
        input_ids: popxl.Tensor,
        dec_input_ids: popxl.Tensor,
        mask: popxl.Tensor,
        dec_mask: popxl.Tensor,
    ):
        # Embeddings
        facts, graph = T5EmbeddingsTP(self.config).create_graph(input_ids.spec)
        embedding_weights = self.add_variable_inputs("embeddings", facts)
        (x,) = graph.bind(embedding_weights).call(input_ids)

        # Encoder stack
        x = self.encoder(x, mask)
        x = self.encoder_head(x)

        # Decoder embeddings
        word_embedding = embedding_weights.word.weight
        x_dec = self.dec_embeddings(dec_input_ids, word_embedding)

        # Decoder stack
        x = self.decoder(x_dec, dec_mask, x, mask)

        if self.include_layer_norm:
            x = self.ln_f(x)
        return x

    @staticmethod
    def hf_mapping(
        config: T5Config, variables: NamedTensors, hf_model: HFModel, layer_norm=True
    ) -> Dict[popxl.Tensor, np.ndarray]:
        weights = {}
        # Embedding weights
        weights.update(T5EmbeddingsTP.hf_mapping(config, variables.embeddings, hf_model))
        # Encoder + decoder weights
        for l in range(config.model.layers):
            weights.update(T5EncoderBlockTP.hf_mapping(config, variables.encoder[l], hf_model.encoder.block[l]))
            weights.update(T5DecoderBlockTP.hf_mapping(config, variables.decoder[l], hf_model.decoder.block[l]))
        # Final layer norms
        weights.update(T5EncoderHead.hf_mapping(config, variables.encoder_head, hf_model.encoder))
        if layer_norm:
            weights.update(T5LayerNorm.hf_mapping(config, variables.ln_f, hf_model.decoder.final_layer_norm))
        return weights

    @staticmethod
    def to_hf(variables_data: NamedTensorData, hf_model: HFModel, layer_norm=True) -> Dict[str, torch.Tensor]:
        config = hf_model.config
        state_dict = {}
        # Embedding weights
        state_dict.update(T5EmbeddingsTP.to_hf(config, variables_data.embeddings, hf_model.shared))
        # The embedding weights are shared between the encoder and decoder, but we need
        # to provide an entry in the dict for both, otherwise load_state_dict() fails
        state_dict["encoder.embed_tokens.weight"] = torch.tensor(
            np.concatenate(variables_data.embeddings.word.weight, axis=0)[: config.vocab_size], dtype=config.torch_dtype
        )
        state_dict["decoder.embed_tokens.weight"] = torch.tensor(
            np.concatenate(variables_data.embeddings.word.weight, axis=0)[: config.vocab_size], dtype=config.torch_dtype
        )
        # Encoder + decoder weights
        for l in range(config.num_layers):
            state_dict.update(
                {
                    f"encoder.block.{l}.{k}": v
                    for k, v in T5EncoderBlockTP.to_hf(
                        config, variables_data.encoder[l], hf_model.encoder.block[l]
                    ).items()
                }
            )
            state_dict.update(
                {
                    f"decoder.block.{l}.{k}": v
                    for k, v in T5DecoderBlockTP.to_hf(
                        config, variables_data.decoder[l], hf_model.decoder.block[l]
                    ).items()
                }
            )
        # Final layer norms
        state_dict.update(T5EncoderHead.to_hf(config, variables_data.encoder_head, hf_model.encoder))
        if layer_norm:
            dec_ln_f = T5LayerNorm.to_hf(config, variables_data.ln_f, hf_model.decoder.final_layer_norm)
            state_dict.update({"decoder.final_layer_norm." + k: v for k, v in dec_ln_f.items()})
        return state_dict
