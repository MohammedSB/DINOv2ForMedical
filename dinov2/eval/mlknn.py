# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import argparse
from functools import partial
import json
import logging
import os
import sys
from typing import List, Optional


import numpy as np
from scipy import sparse
from sklearn import multiclass
import sklearn.metrics 
from sklearn.datasets import make_multilabel_classification
from sklearn.neighbors import NearestNeighbors

from skmultilearn.utils import get_matrix_in_format

import torch
from torch.nn.functional import one_hot, softmax

import dinov2.distributed as distributed
from dinov2.data import SamplerType, make_data_loader, make_dataset
from dinov2.data.transforms import make_classification_eval_transform
from dinov2.eval.metrics import MetricAveraging, build_topk_accuracy_metric
from dinov2.eval.setup import get_args_parser as get_setup_args_parser
from dinov2.eval.setup import setup_and_build_model
from dinov2.eval.utils import ModelWithNormalize, MLkNN, evaluate, extract_features

logger = logging.getLogger("dinov2")


def get_args_parser(
    description: Optional[str] = None,
    parents: Optional[List[argparse.ArgumentParser]] = [],
    add_help: bool = True,
):
    setup_args_parser = get_setup_args_parser(parents=parents, add_help=False)
    parents = [setup_args_parser]
    parser = argparse.ArgumentParser(
        description=description,
        parents=parents,
        add_help=add_help,
    )
    parser.add_argument(
        "--train-dataset",
        dest="train_dataset_str",
        type=str,
        help="Training dataset",
    )
    parser.add_argument(
        "--val-dataset",
        dest="val_dataset_str",
        type=str,
        help="Validation dataset",
    )
    parser.add_argument(
        "--nb_knn",
        nargs="+",
        type=int,
        help="Number of NN to use. 20 is usually working the best.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        help="Temperature used in the voting coefficient",
    )
    parser.add_argument(
        "--gather-on-cpu",
        action="store_true",
        help="Whether to gather the train features on cpu, slower"
        "but useful to avoid OOM for large datasets (e.g. ImageNet22k).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        help="Batch size.",
    )
    parser.add_argument(
        "--n-per-class-list",
        nargs="+",
        type=int,
        help="Number to take per class",
    )
    parser.add_argument(
        "--n-tries",
        type=int,
        help="Number of tries",
    )
    parser.set_defaults(
        train_dataset_str="ImageNet:split=TRAIN",
        val_dataset_str="ImageNet:split=VAL",
        nb_knn=[5],
        temperature=0.07,
        batch_size=16,
        n_per_class_list=[-1],
        n_tries=1,
    )
    return parser



def filter_train(mapping, n_per_class, seed):
    torch.manual_seed(seed)
    final_indices = []
    for k in mapping.keys():
        index = torch.randperm(len(mapping[k]))[:n_per_class]
        final_indices.append(mapping[k][index])
    return torch.cat(final_indices).squeeze()


def create_class_indices_mapping(labels):
    unique_labels, inverse = torch.unique(labels, return_inverse=True)
    mapping = {unique_labels[i]: (inverse == i).nonzero() for i in range(len(unique_labels))}
    return mapping


class ModuleDictWithForward(torch.nn.ModuleDict):
    def forward(self, *args, **kwargs):
        return {k: module(*args, **kwargs) for k, module in self._modules.items()}


def eval_knn(
    model,
    train_dataset,
    val_dataset,
    accuracy_averaging,
    nb_knn,
    temperature,
    batch_size,
    num_workers,
    gather_on_cpu,
    n_per_class_list=[-1],
    n_tries=1,
):
    model = ModelWithNormalize(model)

    logger.info("Extracting features for train set...")
    train_features, train_labels = extract_features(
        model, train_dataset, batch_size, num_workers, gather_on_cpu=gather_on_cpu
    )
    logger.info(f"Train features created, shape {train_features.shape}.")

    model.eval()
    logger.info("Extracting features for evaluation set...")
    val_features, val_labels = extract_features(
        model, val_dataset, batch_size, num_workers, gather_on_cpu=gather_on_cpu
    )

    train_features, train_labels = train_features.cpu().numpy(), train_labels.cpu().numpy()
    val_features, val_labels = val_features.cpu().numpy(), val_labels.cpu().numpy()

    results_dict = {}
    # ============ evaluation ... ============
    logger.info("Start the Multilabel k-NN classification.")
    for k in nb_knn:

        results_dict[f"{k}"] = {}

        classifier = MLkNN(k)
        classifier.fit(train_features, train_labels)
        results = classifier.predict(val_features).toarray()
        
        results_dict[f"{k}"]["Hamming Loss"]  = sklearn.metrics.hamming_loss(val_labels, results)
        results_dict[f"{k}"]["Accuracy"]  = sklearn.metrics.accuracy_score(val_labels, results)
        results_dict[f"{k}"]["mAUC Combined"]  = sklearn.metrics.roc_auc_score(val_labels, results, average="macro")
        results_dict[f"{k}"]["F1"]  = sklearn.metrics.f1_score(val_labels, results, average="macro")

        # Disease-specific scores
        disease_results = {"AUC": {}, "Accuracy": {}, "F1": {}}
        for index, disease in enumerate(train_dataset.class_names):
            disease_results["AUC"][disease] =  sklearn.metrics.roc_auc_score(val_labels[:, index], results[:, index])
            disease_results["Accuracy"][disease] =  sklearn.metrics.accuracy_score(val_labels[:, index], results[:, index])
            disease_results["F1"][disease] =  sklearn.metrics.f1_score(val_labels[:, index], results[:, index])

        results_dict[f"{k}"]["Disease-specific"] = disease_results

    return results_dict


def eval_knn_with_model(
    model,
    output_dir,
    train_dataset_str="ImageNet:split=TRAIN",
    val_dataset_str="ImageNet:split=VAL",
    nb_knn=(5, 20, 50, 100, 200),
    temperature=0.07,
    autocast_dtype=torch.float,
    accuracy_averaging=MetricAveraging.MEAN_ACCURACY,
    transform=None,
    gather_on_cpu=False,
    batch_size=256,
    num_workers=5,
    n_per_class_list=[-1],
    n_tries=1,
):
    
    transform = transform or make_classification_eval_transform()

    train_dataset = make_dataset(
        dataset_str=train_dataset_str,
        transform=transform,
    )
    val_dataset = make_dataset(
        dataset_str=val_dataset_str,
        transform=transform,
    )

    with torch.cuda.amp.autocast(dtype=autocast_dtype):
        results_dict_knn = eval_knn(
            model=model,
            train_dataset=train_dataset,
            val_dataset=val_dataset,
            accuracy_averaging=accuracy_averaging,
            nb_knn=nb_knn,
            temperature=temperature,
            batch_size=batch_size,
            num_workers=num_workers,
            gather_on_cpu=gather_on_cpu,
            n_per_class_list=n_per_class_list,
            n_tries=n_tries,
        )

    metrics_file_path = os.path.join(output_dir, "results_eval_knn.json")
    with open(metrics_file_path, "a") as f:
        for k, v in results_dict_knn.items():
            f.write(json.dumps({k: v}) + "\n")

    if distributed.is_enabled():
        torch.distributed.barrier()
    return results_dict_knn


def main(args):
    model, autocast_dtype = setup_and_build_model(args)
    eval_knn_with_model(
        model=model,
        output_dir=args.output_dir,
        train_dataset_str=args.train_dataset_str,
        val_dataset_str=args.val_dataset_str,
        nb_knn=args.nb_knn,
        temperature=args.temperature,
        autocast_dtype=autocast_dtype,
        accuracy_averaging=MetricAveraging.MEAN_ACCURACY,
        transform=None,
        gather_on_cpu=args.gather_on_cpu,
        batch_size=args.batch_size,
        num_workers=2,
        n_per_class_list=args.n_per_class_list,
        n_tries=args.n_tries,
    )
    return 0

if __name__ == "__main__":
    description = "DINOv2 Multilabel k-NN evaluation"
    args_parser = get_args_parser(description=description)
    args = args_parser.parse_args()
    sys.exit(main(args))
