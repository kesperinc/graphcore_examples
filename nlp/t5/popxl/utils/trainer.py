# Copyright (c) 2023 Graphcore Ltd. All rights reserved.
from typing import Union, Optional, Callable

from config import T5Config
from datasets import Metric
from transformers import T5TokenizerFast
from transformers.models.t5 import T5ForConditionalGeneration

from finetuning import finetuning
from run_finetuning import training
from run_validation import run_validation
from inference import inference
from modelling.hf_mapping import hf_mapping_lm_tp

from modelling.hf_mapping import load_lm_to_hf
from torch.utils.data import Dataset
from popxl_addons.utils import timer
import logging


class T5Trainer:
    def __init__(
        self,
        config: T5Config = None,
        pretrained: Union[T5ForConditionalGeneration, str] = None,
        dataset: Dataset = None,
        eval_dataset: Optional[Dataset] = None,
        eval_config: Optional[T5Config] = None,
        tokenizer: Optional[T5TokenizerFast] = None,
        metric: Optional[Metric] = None,
        process_answers_func: Optional[Callable] = None,
    ):
        """
        Creates a trainer for a T5 model.
        Args:
            - config: the training configuration.
            - pretrained: the Hugging Face pre-trained model, used to initialise the weights.
            - dataset: the training dataset.
            - eval_dataset: the validation dataset.
            - eval_config: the inference configuration, to be used in validation.
            - tokenizer: the tokenizer, needed during validation.
            - metric: the metric for validation. An Hugging Face metric from the `evaluate` module. We use the `accuracy` metric.
            - process_answers_func: a function to convert the generated answers to the format required by the metric and the labels.
            For example in textual entailment we need to convert the answers `[entailment, contradiction, neutral]` to indices.

            Note that these arguments can also be provided later on when calling `train(...)` or `evaluate(...)`.
        """
        self.config = config
        self.train_session = None
        self.pretrained = pretrained
        self.dataset = dataset

        self.eval_config = eval_config
        self.tokenizer = tokenizer
        self.eval_dataset = eval_dataset
        self.inference_session = None
        self.metric = metric
        self.process_answers_func = process_answers_func

    def train(
        self,
        config: T5Config = None,
        pretrained: Union[T5ForConditionalGeneration, str] = None,
        dataset: Dataset = None,
    ):
        """
        Perform finetuning of the T5 model with the given dataset.
        Args:
            - config: the training configuration.
            - pretrained: the Hugging Face pre-trained model, used to initialise the weights.
            - dataset: the training dataset.

            Note that if these arguments have been already provided in the `__init__(...)` then you don't need to provide them again here.
        """
        if config:
            self.config = config
        assert self.config is not None, "A config must be provided for finetuning"

        if pretrained:
            self.pretrained = pretrained
        assert self.pretrained is not None, "A pretrained model must be provided for finetuning"

        if dataset:
            self.dataset = dataset
        assert self.dataset is not None, "A dataset must be provided for finetuning"

        self.train_session = self.__build_train_session()
        with self.train_session:
            training(self.config, self.train_session, self.pretrained, self.dataset)

    def evaluate(
        self,
        eval_dataset: Optional[Dataset] = None,
        eval_config: Optional[T5Config] = None,
        tokenizer: Optional[T5TokenizerFast] = None,
        metric: Optional[Metric] = None,
        process_answers_func: Optional[Callable] = None,
        label_name: str = "label",
        use_pretrained: bool = False,
    ):
        """
        Perform evaluation of the finetuned T5 model with the given validation dataset.
        Args:
            - eval_dataset: the validation dataset.
            - eval_config: the inference configuration, to be used in validation.
            - tokenizer: the tokenizer, needed during validation.
            - metric: the metric for validation. An Hugging Face metric from the `evaluate` module. We use the `accuracy` metric.
            - process_answers_func: a function to convert the generated answers to the format required by the metric and the labels.
            For example in textual entailment we need to convert the answers `[entailment, contradiction, neutral]` to indices.
            - label_name: the name of the eval_dataset's column to be used as groud truth labels.
            - use_pretrained: use the weights from the initially provided pretrained model instead of the finetuned weights.

            Note that if these arguments have been already provided in the `__init__(...)` then you don't need to provide them again here.

        Returns:
            - The metrics computed from the evaluation.
        """
        if tokenizer:
            self.tokenizer = tokenizer
        assert self.tokenizer is not None, "A tokenizer must be provided for evaluation"

        if eval_dataset:
            self.eval_dataset = eval_dataset
        assert self.eval_dataset is not None, "A dataset must be provided for evaluation"

        if metric:
            self.metric = metric
        assert self.metric is not None, "A metric must be provided for evaluation"

        if eval_config:
            self.eval_config = eval_config
        assert self.eval_config is not None, "A config for eval must be provided for evaluation"

        self.inference_session = self.__build_inference_session()

        if process_answers_func:
            self.process_answers_func = process_answers_func

        with self.inference_session:
            # - If we have compiled the training model and we don't specify an `eval_config.checkpoint.load` folder, the latest training weights will be used.
            # - If we instead specify a folder in `eval_config.checkpoint.load`, we will use the weights from that checkpoint.
            # - If neither of the above is true or if `use_pretrained == True`, we will use the weights from `pretrained`.
            if (self.train_session is None and self.eval_config.checkpoint.load is None) or use_pretrained:
                with timer("Loading HF pretrained model to IPU"):
                    self.inference_session.write_variables_data(
                        hf_mapping_lm_tp(self.eval_config, self.inference_session, self.pretrained)
                    )
                train_session = None
            else:
                train_session = self.train_session

            raw_answers = run_validation(
                self.eval_config, self.inference_session, self.eval_dataset, self.tokenizer, train_session
            )

            if self.process_answers_func is not None:
                formatted_answers = self.process_answers_func(raw_answers)
            else:
                formatted_answers = raw_answers

        metrics = self.metric.compute(predictions=formatted_answers, references=self.eval_dataset[label_name])
        logging.info(metrics)

        return metrics

    def save_hf_checkpoint(self, hf_ckpt_dir: str, ckpt_load_path: Optional[str] = None) -> T5ForConditionalGeneration:
        """
        Saves a checkpoint in Hugging Face format, which can then be loaded using Hugging Face API:
            ```
            AutoModelForCausalLM.from_pretrained(hf_ckpt_dir)
            ```
        Args:
            - hf_ckpt_dir (str): path to save the Hugging Face checkpoint
            - ckpt_load_path (str, Optional): path of a specific checkpoint. Default is None, meaning that the latest weights are saved.

        Returns:
            - T5ForConditionalGeneration: finetuned Hugging Face model
        """
        self.train_session.state = self.train_session.state.fwd
        if ckpt_load_path:
            self.train_session.load_checkpoint(ckpt_load_path)
        finetuned = load_lm_to_hf(self.train_session, self.pretrained)
        finetuned.save_pretrained(hf_ckpt_dir)
        return finetuned

    def __build_train_session(self):
        if self.train_session is None:
            self.train_session = finetuning(self.config)
        return self.train_session

    def __build_inference_session(self):
        if self.inference_session is None:
            self.inference_session = inference(self.eval_config)
        return self.inference_session
