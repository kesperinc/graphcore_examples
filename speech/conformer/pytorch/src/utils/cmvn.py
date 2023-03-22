# Copyright (c) 2021 Graphcore Ltd. All rights reserved.
# Copyright 2021 Mobvoi Inc. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This script has been adapted from some of the original WeNet repo found here:
[
    https://github.com/wenet-e2e/wenet/blob/main/wenet/utils/cmvn.py
]
"""

import json
import math

import numpy as np


def _load_json_cmvn(json_cmvn_file):
    """Load the json format cmvn stats file and calculate cmvn

    Args:
        json_cmvn_file: cmvn stats file in json format

    Returns:
        a numpy array of [means, vars]
    """
    with open(json_cmvn_file) as f:
        cmvn_stats = json.load(f)

    means = cmvn_stats["mean_stat"]
    variance = cmvn_stats["var_stat"]
    count = cmvn_stats["frame_num"]
    for i in range(len(means)):
        means[i] /= count
        variance[i] = variance[i] / count - means[i] * means[i]
        if variance[i] < 1.0e-20:
            variance[i] = 1.0e-20
        variance[i] = 1.0 / math.sqrt(variance[i])
    cmvn = np.array([means, variance])
    return cmvn


def _load_kaldi_cmvn(kaldi_cmvn_file):
    """Load the kaldi format cmvn stats file and calculate cmvn

    Args:
        kaldi_cmvn_file:  kaldi text style global cmvn file, which
           is generated by:
           compute-cmvn-stats --binary=false scp:feats.scp global_cmvn

    Returns:
        a numpy array of [means, vars]
    """
    means = []
    variance = []
    with open(kaldi_cmvn_file, "r") as fid:
        # kaldi binary file start with '\0B'
        if fid.read(2) == "\0B":
            logging.error(
                "kaldi cmvn binary file is not supported, please "
                "recompute it by: compute-cmvn-stats --binary=false "
                " scp:feats.scp global_cmvn"
            )
            sys.exit(1)
        fid.seek(0)
        arr = fid.read().split()
        assert arr[0] == "["
        assert arr[-2] == "0"
        assert arr[-1] == "]"
        feat_dim = int((len(arr) - 2 - 2) / 2)
        for i in range(1, feat_dim + 1):
            means.append(float(arr[i]))
        count = float(arr[feat_dim + 1])
        for i in range(feat_dim + 2, 2 * feat_dim + 2):
            variance.append(float(arr[i]))

    for i in range(len(means)):
        means[i] /= count
        variance[i] = variance[i] / count - means[i] * means[i]
        if variance[i] < 1.0e-20:
            variance[i] = 1.0e-20
        variance[i] = 1.0 / math.sqrt(variance[i])
    cmvn = np.array([means, variance])
    return cmvn


def load_cmvn(cmvn_file, is_json):
    if is_json:
        cmvn = _load_json_cmvn(cmvn_file)
    else:
        cmvn = _load_kaldi_cmvn(cmvn_file)
    return cmvn[0], cmvn[1]
