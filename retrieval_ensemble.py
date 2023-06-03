# -*- coding: utf-8 -*-


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
import pickle

import pandas as pd
from tqdm import tqdm
from itertools import combinations
import random
import cv2
from sklearn.utils import shuffle
import pathlib


import numpy as np
from scipy.io import matlab
import tensorflow as tf
from tensorflow.keras.applications import ResNet101V2
from tensorflow.keras import layers, Model
from tensorflow.keras import backend as K
from tensorflow.keras.preprocessing.image import ImageDataGenerator
from tensorflow.keras.utils import Sequence
from tensorflow.keras.layers import Dense, GlobalAveragePooling2D, GlobalMaxPooling2D, Conv2D, Multiply, Add, Activation
from absl import app
from absl import flags


_GROUND_TRUTH_KEYS = ['easy', 'hard', 'junk']

DATASET_NAMES = ['roxford5k', 'rparis6k']

def ReadDatasetFile(dataset_file_path):
  """Reads dataset file in Revisited Oxford/Paris ".mat" format.

  Args:
    dataset_file_path: Path to dataset file, in .mat format.

  Returns:
    query_list: List of query image names.
    index_list: List of index image names.
    ground_truth: List containing ground-truth information for dataset. Each
      entry is a dict corresponding to the ground-truth information for a query.
      The dict may have keys 'easy', 'hard', or 'junk', mapping to a NumPy
      array of integers; additionally, it has a key 'bbx' mapping to a NumPy
      array of floats with bounding box coordinates.
  """
  with tf.io.gfile.GFile(dataset_file_path, 'rb') as f:
    cfg = matlab.loadmat(f)

  # Parse outputs according to the specificities of the dataset file.
  query_list = [str(im_array[0]) for im_array in np.squeeze(cfg['qimlist'])]
  index_list = [str(im_array[0]) for im_array in np.squeeze(cfg['imlist'])]
  ground_truth_raw = np.squeeze(cfg['gnd'])
  ground_truth = []
  for query_ground_truth_raw in ground_truth_raw:
    query_ground_truth = {}
    for ground_truth_key in _GROUND_TRUTH_KEYS:
      if ground_truth_key in query_ground_truth_raw.dtype.names:
        adjusted_labels = query_ground_truth_raw[ground_truth_key] - 1
        query_ground_truth[ground_truth_key] = adjusted_labels.flatten()

    query_ground_truth['bbx'] = np.squeeze(query_ground_truth_raw['bbx'])
    ground_truth.append(query_ground_truth)

  return query_list, index_list, ground_truth


def _ParseGroundTruth(ok_list, junk_list):
  """Constructs dictionary of ok/junk indices for a data subset and query.

  Args:
    ok_list: List of NumPy arrays containing true positive indices for query.
    junk_list: List of NumPy arrays containing ignored indices for query.

  Returns:
    ok_junk_dict: Dict mapping 'ok' and 'junk' strings to NumPy array of
      indices.
  """
  ok_junk_dict = {}
  ok_junk_dict['ok'] = np.concatenate(ok_list)
  ok_junk_dict['junk'] = np.concatenate(junk_list)
  return ok_junk_dict


def ParseEasyMediumHardGroundTruth(ground_truth):
  """Parses easy/medium/hard ground-truth from Revisited datasets.

  Args:
    ground_truth: Usually the output from ReadDatasetFile(). List containing
      ground-truth information for dataset. Each entry is a dict corresponding
      to the ground-truth information for a query. The dict must have keys
      'easy', 'hard', and 'junk', mapping to a NumPy array of integers.

  Returns:
    easy_ground_truth: List containing ground-truth information for easy subset
      of dataset. Each entry is a dict corresponding to the ground-truth
      information for a query. The dict has keys 'ok' and 'junk', mapping to a
      NumPy array of integers.
    medium_ground_truth: Same as `easy_ground_truth`, but for the medium subset.
    hard_ground_truth: Same as `easy_ground_truth`, but for the hard subset.
  """
  num_queries = len(ground_truth)

  easy_ground_truth = []
  medium_ground_truth = []
  hard_ground_truth = []
  for i in range(num_queries):
    easy_ground_truth.append(
        _ParseGroundTruth([ground_truth[i]['easy']],
                          [ground_truth[i]['junk'], ground_truth[i]['hard']]))
    medium_ground_truth.append(
        _ParseGroundTruth([ground_truth[i]['easy'], ground_truth[i]['hard']],
                          [ground_truth[i]['junk']]))
    hard_ground_truth.append(
        _ParseGroundTruth([ground_truth[i]['hard']],
                          [ground_truth[i]['junk'], ground_truth[i]['easy']]))

  return easy_ground_truth, medium_ground_truth, hard_ground_truth


def AdjustPositiveRanks(positive_ranks, junk_ranks):
  """Adjusts positive ranks based on junk ranks.

  Args:
    positive_ranks: Sorted 1D NumPy integer array.
    junk_ranks: Sorted 1D NumPy integer array.

  Returns:
    adjusted_positive_ranks: Sorted 1D NumPy array.
  """
  if not junk_ranks.size:
    return positive_ranks

  adjusted_positive_ranks = positive_ranks
  j = 0
  for i, positive_index in enumerate(positive_ranks):
    while (j < len(junk_ranks) and positive_index > junk_ranks[j]):
      j += 1

    adjusted_positive_ranks[i] -= j

  return adjusted_positive_ranks


def ComputeAveragePrecision(positive_ranks):
  """Computes average precision according to dataset convention.

  It assumes that `positive_ranks` contains the ranks for all expected positive
  index images to be retrieved. If `positive_ranks` is empty, returns
  `average_precision` = 0.

  Note that average precision computation here does NOT use the finite sum
  method (see
  https://en.wikipedia.org/wiki/Evaluation_measures_(information_retrieval)#Average_precision)
  which is common in information retrieval literature. Instead, the method
  implemented here integrates over the precision-recall curve by averaging two
  adjacent precision points, then multiplying by the recall step. This is the
  convention for the Revisited Oxford/Paris datasets.

  Args:
    positive_ranks: Sorted 1D NumPy integer array, zero-indexed.

  Returns:
    average_precision: Float.
  """
  average_precision = 0.0

  num_expected_positives = len(positive_ranks)
  if not num_expected_positives:
    return average_precision

  recall_step = 1.0 / num_expected_positives
  for i, rank in enumerate(positive_ranks):
    if not rank:
      left_precision = 1.0
    else:
      left_precision = i / rank

    right_precision = (i + 1) / (rank + 1)
    average_precision += (left_precision + right_precision) * recall_step / 2

  return average_precision


def ComputePRAtRanks(positive_ranks, desired_pr_ranks):
  """Computes precision/recall at desired ranks.

  It assumes that `positive_ranks` contains the ranks for all expected positive
  index images to be retrieved. If `positive_ranks` is empty, return all-zeros
  `precisions`/`recalls`.

  If a desired rank is larger than the last positive rank, its precision is
  computed based on the last positive rank. For example, if `desired_pr_ranks`
  is [10] and `positive_ranks` = [0, 7] --> `precisions` = [0.25], `recalls` =
  [1.0].

  Args:
    positive_ranks: 1D NumPy integer array, zero-indexed.
    desired_pr_ranks: List of integers containing the desired precision/recall
      ranks to be reported. Eg, if precision@1/recall@1 and
      precision@10/recall@10 are desired, this should be set to [1, 10].

  Returns:
    precisions: Precision @ `desired_pr_ranks` (NumPy array of
      floats, with shape [len(desired_pr_ranks)]).
    recalls: Recall @ `desired_pr_ranks` (NumPy array of floats, with
      shape [len(desired_pr_ranks)]).
  """
  num_desired_pr_ranks = len(desired_pr_ranks)
  precisions = np.zeros([num_desired_pr_ranks])
  recalls = np.zeros([num_desired_pr_ranks])

  num_expected_positives = len(positive_ranks)
  if not num_expected_positives:
    return precisions, recalls

  positive_ranks_one_indexed = positive_ranks + 1
  for i, desired_pr_rank in enumerate(desired_pr_ranks):
    recalls[i] = np.sum(
        positive_ranks_one_indexed <= desired_pr_rank) / num_expected_positives

    # If `desired_pr_rank` is larger than last positive's rank, only compute
    # precision with respect to last positive's position.
    precision_rank = min(max(positive_ranks_one_indexed), desired_pr_rank)
    precisions[i] = np.sum(
        positive_ranks_one_indexed <= precision_rank) / precision_rank

  return precisions, recalls


def ComputeMetrics(sorted_index_ids, ground_truth, desired_pr_ranks):
  """Computes metrics for retrieval results on the Revisited datasets.

  If there are no valid ground-truth index images for a given query, the metric
  results for the given query (`average_precisions`, `precisions` and `recalls`)
  are set to NaN, and they are not taken into account when computing the
  aggregated metrics (`mean_average_precision`, `mean_precisions` and
  `mean_recalls`) over all queries.

  Args:
    sorted_index_ids: Integer NumPy array of shape [#queries, #index_images].
      For each query, contains an array denoting the most relevant index images,
      sorted from most to least relevant.
    ground_truth: List containing ground-truth information for dataset. Each
      entry is a dict corresponding to the ground-truth information for a query.
      The dict has keys 'ok' and 'junk', mapping to a NumPy array of integers.
    desired_pr_ranks: List of integers containing the desired precision/recall
      ranks to be reported. Eg, if precision@1/recall@1 and
      precision@10/recall@10 are desired, this should be set to [1, 10]. The
      largest item should be <= #index_images.

  Returns:
    mean_average_precision: Mean average precision (float).
    mean_precisions: Mean precision @ `desired_pr_ranks` (NumPy array of
      floats, with shape [len(desired_pr_ranks)]).
    mean_recalls: Mean recall @ `desired_pr_ranks` (NumPy array of floats, with
      shape [len(desired_pr_ranks)]).
    average_precisions: Average precision for each query (NumPy array of floats,
      with shape [#queries]).
    precisions: Precision @ `desired_pr_ranks`, for each query (NumPy array of
      floats, with shape [#queries, len(desired_pr_ranks)]).
    recalls: Recall @ `desired_pr_ranks`, for each query (NumPy array of
      floats, with shape [#queries, len(desired_pr_ranks)]).

  Raises:
    ValueError: If largest desired PR rank in `desired_pr_ranks` >
      #index_images.
  """
  num_queries, num_index_images = sorted_index_ids.shape
  num_desired_pr_ranks = len(desired_pr_ranks)

  sorted_desired_pr_ranks = sorted(desired_pr_ranks)

  if sorted_desired_pr_ranks[-1] > num_index_images:
    raise ValueError(
        'Requested PR ranks up to %d, however there are only %d images' %
        (sorted_desired_pr_ranks[-1], num_index_images))

  # Instantiate all outputs, then loop over each query and gather metrics.
  mean_average_precision = 0.0
  mean_precisions = np.zeros([num_desired_pr_ranks])
  mean_recalls = np.zeros([num_desired_pr_ranks])
  average_precisions = np.zeros([num_queries])
  precisions = np.zeros([num_queries, num_desired_pr_ranks])
  recalls = np.zeros([num_queries, num_desired_pr_ranks])
  num_empty_gt_queries = 0
  for i in range(num_queries):
    ok_index_images = ground_truth[i]['ok']
    junk_index_images = ground_truth[i]['junk']

    if not ok_index_images.size:
      average_precisions[i] = float('nan')
      precisions[i, :] = float('nan')
      recalls[i, :] = float('nan')
      num_empty_gt_queries += 1
      continue

    positive_ranks = np.arange(num_index_images)[np.in1d(
        sorted_index_ids[i], ok_index_images)]
    junk_ranks = np.arange(num_index_images)[np.in1d(sorted_index_ids[i],
                                                     junk_index_images)]

    adjusted_positive_ranks = AdjustPositiveRanks(positive_ranks, junk_ranks)

    average_precisions[i] = ComputeAveragePrecision(adjusted_positive_ranks)
    precisions[i, :], recalls[i, :] = ComputePRAtRanks(adjusted_positive_ranks,
                                                       desired_pr_ranks)

    mean_average_precision += average_precisions[i]
    mean_precisions += precisions[i, :]
    mean_recalls += recalls[i, :]

  # Normalize aggregated metrics by number of queries.
  num_valid_queries = num_queries - num_empty_gt_queries
  mean_average_precision /= num_valid_queries
  mean_precisions /= num_valid_queries
  mean_recalls /= num_valid_queries

  return (mean_average_precision, mean_precisions, mean_recalls,
          average_precisions, precisions, recalls)


def SaveMetricsFile(mean_average_precision, mean_precisions, mean_recalls,
                    pr_ranks, output_path):
  """Saves aggregated retrieval metrics to text file.

  Args:
    mean_average_precision: Dict mapping each dataset protocol to a float.
    mean_precisions: Dict mapping each dataset protocol to a NumPy array of
      floats with shape [len(pr_ranks)].
    mean_recalls: Dict mapping each dataset protocol to a NumPy array of floats
      with shape [len(pr_ranks)].
    pr_ranks: List of integers.
    output_path: Full file path.
  """
  with tf.io.gfile.GFile(output_path, 'w') as f:
    for k in sorted(mean_average_precision.keys()):
      f.write('{}\n  mAP={}\n  mP@k{} {}\n  mR@k{} {}\n'.format(
          k, np.around(mean_average_precision[k] * 100, decimals=2),
          np.array(pr_ranks), np.around(mean_precisions[k] * 100, decimals=2),
          np.array(pr_ranks), np.around(mean_recalls[k] * 100, decimals=2)))


def _ParseSpaceSeparatedStringsInBrackets(line, prefixes, ind):
  """Parses line containing space-separated strings in brackets.

  Args:
    line: String, containing line in metrics file with mP@k or mR@k figures.
    prefixes: Tuple/list of strings, containing valid prefixes.
    ind: Integer indicating which field within brackets is parsed.

  Yields:
    entry: String format entry.

  Raises:
    ValueError: If input line does not contain a valid prefix.
  """
  for prefix in prefixes:
    if line.startswith(prefix):
      line = line[len(prefix):]
      break
  else:
    raise ValueError('Line %s is malformed, cannot find valid prefixes' % line)

  for entry in line.split('[')[ind].split(']')[0].split():
    yield entry


def _ParsePrRanks(line):
  """Parses PR ranks from mP@k line in metrics file.

  Args:
    line: String, containing line in metrics file with mP@k figures.

  Returns:
    pr_ranks: List of integers, containing used ranks.

  Raises:
    ValueError: If input line is malformed.
  """
  return [
      int(pr_rank) for pr_rank in _ParseSpaceSeparatedStringsInBrackets(
          line, ['  mP@k['], 0) if pr_rank
  ]


def _ParsePrScores(line, num_pr_ranks):
  """Parses PR scores from line in metrics file.

  Args:
    line: String, containing line in metrics file with mP@k or mR@k figures.
    num_pr_ranks: Integer, number of scores that should be in output list.

  Returns:
    pr_scores: List of floats, containing scores.

  Raises:
    ValueError: If input line is malformed.
  """
  pr_scores = [
      float(pr_score) for pr_score in _ParseSpaceSeparatedStringsInBrackets(
          line, ('  mP@k[', '  mR@k['), 1) if pr_score
  ]

  if len(pr_scores) != num_pr_ranks:
    raise ValueError('Line %s is malformed, expected %d scores but found %d' %
                     (line, num_pr_ranks, len(pr_scores)))

  return pr_scores


def ReadMetricsFile(metrics_path):
  """Reads aggregated retrieval metrics from text file.

  Args:
    metrics_path: Full file path, containing aggregated retrieval metrics.

  Returns:
    mean_average_precision: Dict mapping each dataset protocol to a float.
    pr_ranks: List of integer ranks used in aggregated recall/precision metrics.
    mean_precisions: Dict mapping each dataset protocol to a NumPy array of
      floats with shape [len(`pr_ranks`)].
    mean_recalls: Dict mapping each dataset protocol to a NumPy array of floats
      with shape [len(`pr_ranks`)].

  Raises:
    ValueError: If input file is malformed.
  """
  with tf.io.gfile.GFile(metrics_path, 'r') as f:
    file_contents_stripped = [l.rstrip() for l in f]

  if len(file_contents_stripped) % 4:
    raise ValueError(
        'Malformed input %s: number of lines must be a multiple of 4, '
        'but it is %d' % (metrics_path, len(file_contents_stripped)))

  mean_average_precision = {}
  pr_ranks = []
  mean_precisions = {}
  mean_recalls = {}
  protocols = set()
  for i in range(0, len(file_contents_stripped), 4):
    protocol = file_contents_stripped[i]
    if protocol in protocols:
      raise ValueError(
          'Malformed input %s: protocol %s is found a second time' %
          (metrics_path, protocol))
    protocols.add(protocol)

    # Parse mAP.
    mean_average_precision[protocol] = float(
        file_contents_stripped[i + 1].split('=')[1]) / 100.0

    # Parse (or check consistency of) pr_ranks.
    parsed_pr_ranks = _ParsePrRanks(file_contents_stripped[i + 2])
    if not pr_ranks:
      pr_ranks = parsed_pr_ranks
    else:
      if parsed_pr_ranks != pr_ranks:
        raise ValueError('Malformed input %s: inconsistent PR ranks' %
                         metrics_path)

    # Parse mean precisions.
    mean_precisions[protocol] = np.array(
        _ParsePrScores(file_contents_stripped[i + 2], len(pr_ranks)),
        dtype=float) / 100.0

    # Parse mean recalls.
    mean_recalls[protocol] = np.array(
        _ParsePrScores(file_contents_stripped[i + 3], len(pr_ranks)),
        dtype=float) / 100.0

  return mean_average_precision, pr_ranks, mean_precisions, mean_recalls


def CreateConfigForTestDataset(dataset, dir_main):
  """Creates the configuration dictionary for the test dataset.

  Args:
    dataset: String, dataset name: either 'roxford5k' or 'rparis6k'.
    dir_main: String, path to the folder containing ground truth files.

  Returns:
    cfg: Dataset configuration in a form of dictionary. The configuration
      includes:
      `gnd_fname` - path to the ground truth file for the dataset,
      `ext` and `qext` - image extensions for the images in the test dataset
      and the query images,
      `dir_data` - path to the folder containing ground truth files,
      `dir_images` - path to the folder containing images,
      `n` and `nq` - number of images and query images in the dataset
      respectively,
      `im_fname` and `qim_fname` - functions providing paths for the dataset
      and query images respectively,
      `dataset` - test dataset name.

  Raises:
    ValueError: If an unknown dataset name is provided as an argument.
  """
  dataset = dataset.lower()

  def _ConfigImname(cfg, i):
    return os.path.join(cfg['dir_images'], cfg['imlist'][i] + cfg['ext'])

  def _ConfigQimname(cfg, i):
    return os.path.join(cfg['dir_images'], cfg['qimlist'][i] + cfg['qext'])

  if dataset not in DATASET_NAMES:
    raise ValueError('Unknown dataset: {}!'.format(dataset))

  # Loading imlist, qimlist, and gnd in configuration as a dictionary.
  gnd_fname = os.path.join(dir_main, 'gnd_{}.pkl'.format(dataset))
  with tf.io.gfile.GFile(gnd_fname, 'rb') as f:
    cfg = pickle.load(f)
  cfg['gnd_fname'] = gnd_fname
  if dataset == 'rparis6k':
    dir_images = 'paris6k_images'
  elif dataset == 'roxford5k':
    dir_images = 'oxford5k_images'

  cfg['ext'] = '.jpg'
  cfg['qext'] = '.jpg'
  cfg['dir_data'] = os.path.join(dir_main)
  cfg['dir_images'] = os.path.join(cfg['dir_data'], dir_images)

  cfg['n'] = len(cfg['imlist'])
  cfg['nq'] = len(cfg['qimlist'])

  cfg['im_fname'] = _ConfigImname
  cfg['qim_fname'] = _ConfigQimname

  cfg['dataset'] = dataset

  return cfg

#cbam
class ChannelAttention_cbam(tf.keras.layers.Layer):
    def __init__(self, ratio=16, dense_initializer='glorot_uniform', bias_initializer='zeros'):
        super(ChannelAttention_cbam, self).__init__()
        self.ratio = ratio
        self.dense_initializer = dense_initializer
        self.bias_initializer = bias_initializer

    def build(self, input_shape):
        self.dense_1 = Dense(input_shape[-1] // self.ratio, activation='relu', kernel_initializer=self.dense_initializer, bias_initializer=self.bias_initializer)
        self.dense_2 = Dense(input_shape[-1], activation='sigmoid', kernel_initializer=self.dense_initializer, bias_initializer=self.bias_initializer)

    def call(self, inputs):
        avg_pool = GlobalAveragePooling2D()(inputs)
        max_pool = GlobalMaxPooling2D()(inputs)
        avg_pool = self.dense_1(avg_pool)
        max_pool = self.dense_1(max_pool)
        avg_pool = self.dense_2(avg_pool)
        max_pool = self.dense_2(max_pool)
        attention = Add()([avg_pool, max_pool])
        return Multiply()([inputs, attention])

class SpatialAttention_cbam(tf.keras.layers.Layer):
    def __init__(self, conv_initializer='glorot_uniform', bias_initializer='zeros'):
        super(SpatialAttention_cbam, self).__init__()
        self.conv_initializer = conv_initializer
        self.bias_initializer = bias_initializer

    def build(self, input_shape):
        self.conv = Conv2D(1, (7, 7), padding='same', activation='sigmoid', kernel_initializer=self.conv_initializer, bias_initializer=self.bias_initializer)

    def call(self, inputs):
        avg_pool = tf.reduce_mean(inputs, axis=-1, keepdims=True)
        max_pool = tf.reduce_max(inputs, axis=-1, keepdims=True)
        attention = tf.concat([avg_pool, max_pool], axis=-1)
        attention = self.conv(attention)
        return Multiply()([inputs, attention])

class CBAM(Model):
    def __init__(self, base_model):
        super(CBAM, self).__init__()
        self.base_model = base_model
        self.channel_attention = ChannelAttention_cbam()
        self.spatial_attention = SpatialAttention_cbam()

    def call(self, inputs):
        x = self.base_model(inputs)
        x = self.channel_attention(x)
        x = self.spatial_attention(x)
        return x

#cbam
def resnet_cbam(input_shape=(300, 300, 3)):
    resnet101v2_base = ResNet101V2(include_top=False, input_shape=input_shape, weights='imagenet')
    for layer in resnet101v2_base.layers:
      layer.trainable = False
    cbam_resnet101v2 = CBAM(resnet101v2_base)

    inputs = tf.keras.Input(shape=input_shape)
    x = cbam_resnet101v2(inputs)
    x = GlobalAveragePooling2D()(x)
    outputs = tf.keras.layers.Dense(2048, activation='relu')(x)


    model = Model(inputs=inputs, outputs=outputs)

    return model

#se
class SEBlock(layers.Layer):
    def __init__(self, input_channels, ratio=16):
        super(SEBlock, self).__init__()
        self.pool = layers.GlobalAveragePooling2D()
        self.fc1 = layers.Dense(input_channels // ratio, activation='relu')
        self.fc2 = layers.Dense(input_channels, activation='sigmoid')

    def call(self, inputs):
        x = self.pool(inputs)
        x = self.fc1(x)
        x = self.fc2(x)
        x = tf.reshape(x, (-1, 1, 1, inputs.shape[-1]))
        return layers.multiply([inputs, x])

#se
def resnet_se(input_shape=(300, 300, 3)):
    resnet101v2_base = ResNet101V2(include_top=False, input_shape=input_shape, weights='imagenet')
    for layer in resnet101v2_base.layers:
        layer.trainable = False
    final_feature_map = resnet101v2_base.output

    input_channels = final_feature_map.shape[-1]
    se_block = SEBlock(input_channels)
    se_output = se_block(final_feature_map)
    x = GlobalAveragePooling2D()(se_output)
    outputs = Dense(2048, activation='relu')(x)

    model = Model(inputs=resnet101v2_base.input, outputs=outputs)

    return model

#Channel
class ChannelAttention_ch(layers.Layer):
    def __init__(self, units, reduction_ratio=16):
        super(ChannelAttention_ch, self).__init__()
        self.reduction_ratio = reduction_ratio
        self.units = units

    def build(self, input_shape):
        self.dense1 = layers.Dense(self.units // self.reduction_ratio, activation='relu')
        self.dense2 = layers.Dense(self.units, activation='sigmoid')

    def call(self, inputs):
        avg_pool = tf.reduce_mean(inputs, axis=[1, 2], keepdims=True)
        max_pool = tf.reduce_max(inputs, axis=[1, 2], keepdims=True)

        avg_pool = self.dense2(self.dense1(avg_pool))
        max_pool = self.dense2(self.dense1(max_pool))

        attention = tf.add(avg_pool, max_pool)
        return attention * inputs

#channel
def Subnet_ch(input_shape, weights):
  
  backbone_model = ResNet101V2(input_shape=input_shape, include_top=False, weights=weights)

  for layer in backbone_model.layers:
      layer.trainable = False

  conv2_x_out = backbone_model.get_layer('conv2_block3_out').output
  conv3_x_out = backbone_model.get_layer('conv3_block4_out').output
  conv4_x_out = backbone_model.get_layer('conv4_block23_out').output
  backbone_out = backbone_model.output

  GAP_out = tf.keras.layers.GlobalAveragePooling2D()(backbone_out)

  dense1_out = tf.keras.layers.Dense(1024, activation=tf.nn.relu, name='dense_1')(GAP_out)
  dense2_out = tf.keras.layers.Dense(512, activation=tf.nn.relu, name='dense_2')(dense1_out)
  dense3_out = tf.keras.layers.Dense(256, activation=tf.nn.relu, name='dense_3')(dense2_out)

  attn1_out = ChannelAttention_ch(units=conv2_x_out.shape[-1])(conv2_x_out)
  attn2_out = ChannelAttention_ch(units=conv3_x_out.shape[-1])(conv3_x_out)
  attn3_out = ChannelAttention_ch(units=conv4_x_out.shape[-1])(conv4_x_out)

  concat_out = tf.concat([tf.reduce_mean(attn1_out, axis=[1, 2]), tf.reduce_mean(attn2_out, axis=[1, 2]), tf.reduce_mean(attn3_out, axis=[1, 2])], axis=1)

  subnet_model = Model(backbone_model.input, concat_out)


  return subnet_model

#sp
def Subnet_sp(input_shape, weights):
  backbone_model = ResNet101V2(input_shape=input_shape, include_top=False, weights=weights)

  for layer in backbone_model.layers:
    layer.trainable=False

  conv2_x_out = backbone_model.get_layer('conv2_block3_out').output
  conv3_x_out = backbone_model.get_layer('conv3_block4_out').output
  conv4_x_out = backbone_model.get_layer('conv4_block23_out').output
  backbone_out = backbone_model.output

  # print(conv2_x_out.shape, conv3_x_out.shape,conv4_x_out.shape, backbone_out.shape)

  GAP_out = tf.keras.layers.GlobalAveragePooling2D()(backbone_out)

  dense1_out = tf.keras.layers.Dense(1024, activation=tf.nn.relu, name='dense_1')(GAP_out)
  dense2_out = tf.keras.layers.Dense(512, activation=tf.nn.relu, name='dense_2')(dense1_out)
  dense3_out = tf.keras.layers.Dense(256, activation=tf.nn.relu, name='dense_3')(dense2_out)

  dense1_exp_out = tf.expand_dims(dense1_out, 1)
  dense2_exp_out = tf.expand_dims(dense2_out, 1)
  dense3_exp_out = tf.expand_dims(dense3_out, 1)

  # print(dense1_exp_out.shape,dense2_exp_out.shape,dense3_exp_out.shape)

  conv2_x_resh_out = tf.reshape(conv2_x_out, (-1, conv2_x_out.shape[1]*conv2_x_out.shape[2], conv2_x_out.shape[3]))
  conv3_x_resh_out = tf.reshape(conv3_x_out, (-1, conv3_x_out.shape[1]*conv3_x_out.shape[2], conv3_x_out.shape[3]))
  conv4_x_resh_out = tf.reshape(conv4_x_out, (-1, conv4_x_out.shape[1]*conv4_x_out.shape[2], conv4_x_out.shape[3]))

  # print(conv2_x_resh_out.shape, conv3_x_resh_out.shape, conv4_x_resh_out.shape)

  attn1_out = tf.keras.layers.Attention()([dense3_exp_out, conv2_x_resh_out])
  attn2_out = tf.keras.layers.Attention()([dense2_exp_out, conv3_x_resh_out])
  attn3_out = tf.keras.layers.Attention()([dense1_exp_out, conv4_x_resh_out])

  # print(attn1_out.shape, attn2_out.shape, attn3_out.shape)

  concat_out = tf.concat([tf.squeeze(attn1_out, axis=1), tf.squeeze(attn2_out, axis=1), tf.squeeze(attn3_out, axis=1)], axis=1)
  # print(concat_out.shape)

  subnet_model = Model(backbone_model.input, concat_out)


  return subnet_model

def Siamese_se(input_shape, weights):

  # Define the two input layers
  input_1 = tf.keras.layers.Input(shape=input_shape)
  input_2 = tf.keras.layers.Input(shape=input_shape)
  
  # Define the Subnet model
  # subnet = Subnet(input_shape, weights)
  subnet_se = resnet_se(input_shape)

  # Get the output of the Subnet model for the two input layers
  output_1 = subnet_se(input_1)
  output_2 = subnet_se(input_2)

  # Define the L2 distance layer
  dist_layer = tf.keras.layers.Lambda(lambda x: K.sqrt(K.sum(K.square(x[0] - x[1]), axis=-1, keepdims=False)))
  distance = dist_layer([output_1, output_2])

  # Define the Siamese model
  siamese_model = Model(inputs=[input_1, input_2], outputs=distance)
  
  # Compile the Siamese model
  optimizer = tf.keras.optimizers.Adam(learning_rate=0.001, beta_1=0.9, beta_2=0.999, epsilon=1e-07)
  siamese_model.compile(loss=contrastive_loss, optimizer=optimizer, metrics=[contrastive_accuracy])
  
  return siamese_model

def Siamese_cbam(input_shape, weights):

  # Define the two input layers
  input_1 = tf.keras.layers.Input(shape=input_shape)
  input_2 = tf.keras.layers.Input(shape=input_shape)
  
  # Define the Subnet model
  # subnet = Subnet(input_shape, weights)
  subnet_cbam = resnet_cbam(input_shape)

  # Get the output of the Subnet model for the two input layers
  output_1 = subnet_cbam(input_1)
  output_2 = subnet_cbam(input_2)

  # Define the L2 distance layer
  dist_layer = tf.keras.layers.Lambda(lambda x: K.sqrt(K.sum(K.square(x[0] - x[1]), axis=-1, keepdims=False)))
  distance = dist_layer([output_1, output_2])

  # Define the Siamese model
  siamese_model = Model(inputs=[input_1, input_2], outputs=distance)
  
  # Compile the Siamese model
  optimizer = tf.keras.optimizers.Adam(learning_rate=0.001, beta_1=0.9, beta_2=0.999, epsilon=1e-07)
  siamese_model.compile(loss=contrastive_loss, optimizer=optimizer, metrics=[contrastive_accuracy])
  
  return siamese_model

def Siamese_ch(input_shape, weights):

  # Define the two input layers
  input_1 = tf.keras.layers.Input(shape=input_shape)
  input_2 = tf.keras.layers.Input(shape=input_shape)
  
  # Define the Subnet model
  # subnet = Subnet(input_shape, weights)
  subnet_ch = Subnet_ch(input_shape, weights)


  # Get the output of the Subnet model for the two input layers
  output_1 = subnet_ch(input_1)
  output_2 = subnet_ch(input_2)

  # Define the L2 distance layer
  dist_layer = tf.keras.layers.Lambda(lambda x: K.sqrt(K.sum(K.square(x[0] - x[1]), axis=-1, keepdims=False)))
  distance = dist_layer([output_1, output_2])

  # Define the Siamese model
  siamese_model = Model(inputs=[input_1, input_2], outputs=distance)
  
  # Compile the Siamese model
  optimizer = tf.keras.optimizers.Adam(learning_rate=0.001, beta_1=0.9, beta_2=0.999, epsilon=1e-07)
  siamese_model.compile(loss=contrastive_loss, optimizer=optimizer, metrics=[contrastive_accuracy])
  
  return siamese_model

def Siamese_sp(input_shape, weights):

  # Define the two input layers
  input_1 = tf.keras.layers.Input(shape=input_shape)
  input_2 = tf.keras.layers.Input(shape=input_shape)
  
  # Define the Subnet model
  # subnet = Subnet(input_shape, weights)
  subnet_sp = Subnet_sp(input_shape, weights)

  # Get the output of the Subnet model for the two input layers
  output_1 = subnet_sp(input_1)
  output_2 = subnet_sp(input_2)

  # Define the L2 distance layer
  dist_layer = tf.keras.layers.Lambda(lambda x: K.sqrt(K.sum(K.square(x[0] - x[1]), axis=-1, keepdims=False)))
  distance = dist_layer([output_1, output_2])

  # Define the Siamese model
  siamese_model = Model(inputs=[input_1, input_2], outputs=distance)
  
  # Compile the Siamese model
  optimizer = tf.keras.optimizers.Adam(learning_rate=0.001, beta_1=0.9, beta_2=0.999, epsilon=1e-07)
  siamese_model.compile(loss=contrastive_loss, optimizer=optimizer, metrics=[contrastive_accuracy])
  
  return siamese_model

def contrastive_loss(y_true, pred_dist, margin=1.0):
  loss = K.mean(y_true * K.square(pred_dist) + (1 - y_true) * K.square(K.maximum(margin - pred_dist, 0)))
  return loss

def contrastive_accuracy(y_true, pred_dist):
  accuracy = K.mean(K.equal(y_true, K.cast(pred_dist < 0.5, y_true.dtype)))
  return accuracy

# def compute_embeddings(model, image_paths):
#     images = [process_image(path) for path in image_paths]
#     images = np.array(images)
#     embeddings = model.predict(images)
#     return embeddings

def process_image(file_path, bbox=None):
    # Read and decode the image
    img = tf.io.read_file(file_path)
    img = tf.image.decode_jpeg(img, channels=3)
    
    if bbox is not None:
      # Crop the image using the provided bounding box
      bbox = tf.cast(tf.round(bbox), tf.int32)
      img = tf.image.crop_to_bounding_box(img, bbox[1], bbox[0], bbox[3] - bbox[1], bbox[2] - bbox[0])
    
    # Resize the cropped image to a fixed size
    img = tf.image.resize(img, [300, 300])
    
    # Normalize the pixel values to [0, 1]
    img /= 255.0

    return img

def compute_embeddings(model, image_paths, ground_truth=None):
    if ground_truth is not None:
      images = [process_image(path, bbox =[int(round(b)) for b in ground_truth[i]['bbx']]) for i, path in tqdm(enumerate(image_paths), total=len(image_paths))]
    else:
      images = [process_image(path) for path in tqdm(image_paths, total=len(image_paths))]
    images = np.array(images)
    embeddings = model.predict(images)
    return embeddings

input_shape = (300, 300, 3)
weights = None

FLAGS = flags.FLAGS

flags.DEFINE_string(
    'dataset_file_path', './gnd_roxford5k.mat',
    'Dataset file for Revisited Oxford or Paris dataset, in .mat format.')

flags.DEFINE_string(
    'output_dir', './eval/',
    'Directory where retrieval output will be written to. A file containing '
    "metrics for this run is saved therein, with file name 'metrics.txt'.")

flags.DEFINE_string(
    'images_dir', './oxford5k_images',
    'Directory where dataset images are located, all in .jpg format.')


# Extensions.
_IMAGE_EXTENSION = '.jpg'

# Precision-recall ranks to use in metric computation.
_PR_RANKS = (1, 5, 10)

# Pace to log.
_STATUS_CHECK_LOAD_ITERATIONS = 50

# Output file names.
_METRICS_FILENAME = 'metrics.txt'

def main(argv):
  if len(argv) > 1:
    raise RuntimeError('Too many command-line arguments.')
    
  idx = 0;
  
  # Parse dataset to obtain query/index images, and ground-truth.
  print('Parsing dataset...')
  query_list, index_list, ground_truth = ReadDatasetFile(
      FLAGS.dataset_file_path)
  num_query_images = len(query_list)
  num_index_images = len(index_list)
  (easy_ground_truth, medium_ground_truth,
   hard_ground_truth) = ParseEasyMediumHardGroundTruth(ground_truth)
  print('done! Found %d queries and %d index images' %
        (num_query_images, num_index_images))
  
  ################
  #siamese_model_se = Siamese_se(input_shape, weights)
  #siamese_model_se.load_weights('../model_weights/siamese_weights_101_se.h5')

  #siamese_model_cbam = Siamese_cbam(input_shape, weights)
  #siamese_model_cbam.load_weights('../model_weights/siamese_weights_101_cbam.h5')

  #siamese_model_ch = Siamese_ch(input_shape, weights)
  #siamese_model_ch.load_weights('../model_weights/siamese_weights_101_valc.h5')

  #siamese_model_sp = Siamese_sp(input_shape, weights)
  #siamese_model_sp.load_weights('../model_weights/siamese_weights_101_valcon2.h5')
  #print('weights_loaded!')


  # List all the image file paths in the query images folder
  query_list_new = [os.path.join(FLAGS.images_dir, img + _IMAGE_EXTENSION) for img in query_list] 

  # List all the image file paths in the index images folder
  index_list_new = [os.path.join(FLAGS.images_dir, img + _IMAGE_EXTENSION) for img in index_list]
  
  query_img = query_list_new[idx];

  #subnet_model_se = siamese_model_se.layers[2]
  #subnet_model_cbam = siamese_model_cbam.layers[2]
  #subnet_model_ch = siamese_model_ch.layers[2]
  #subnet_model_sp = siamese_model_sp.layers[2]

  # Compute the embeddings for query and index images
  #query_embeddings_se = compute_embeddings(subnet_model_se, query_list_new, ground_truth)
  #index_embeddings_se = compute_embeddings(subnet_model_se, index_list_new)

  #query_embeddings_cbam = compute_embeddings(subnet_model_cbam, query_list_new, ground_truth)
  #index_embeddings_cbam = compute_embeddings(subnet_model_cbam, index_list_new)
  
  #query_embeddings_ch = compute_embeddings(subnet_model_ch, query_list_new, ground_truth)
  #index_embeddings_ch = compute_embeddings(subnet_model_ch, index_list_new)
  
  #query_embeddings_sp = compute_embeddings(subnet_model_sp, query_list_new, ground_truth)
  #index_embeddings_sp = compute_embeddings(subnet_model_sp, index_list_new)
  
  
  query_embeddings_se = np.load('query_embeddings_se.npy')
  index_embeddings_se = np.load('index_embeddings_se.npy')

  query_embeddings_cbam = np.load('query_embeddings_cbam.npy')
  index_embeddings_cbam = np.load('index_embeddings_cbam.npy')

  query_embeddings_ch = np.load('query_embeddings_ch.npy')
  index_embeddings_ch = np.load('index_embeddings_ch.npy')

  #query_embeddings_sp = np.load('query_embeddings_sp.npy')
  #index_embeddings_sp = np.load('index_embeddings_sp.npy')

  # Concatenate embeddings from all Siamese networks
  #query_embeddings = np.concatenate([query_embeddings_se, query_embeddings_cbam, query_embeddings_ch, query_embeddings_sp], axis=-1)
  #index_embeddings = np.concatenate([index_embeddings_se, index_embeddings_cbam, index_embeddings_ch, index_embeddings_sp], axis=-1)
  
  query_embeddings = np.concatenate([query_embeddings_se, query_embeddings_cbam, query_embeddings_ch], axis=-1)
  index_embeddings = np.concatenate([index_embeddings_se, index_embeddings_cbam, index_embeddings_ch], axis=-1)
  
  #query_embeddings = np.concatenate([query_embeddings_se, query_embeddings_cbam], axis=-1)
  #index_embeddings = np.concatenate([index_embeddings_se, index_embeddings_cbam], axis=-1)


  # Compute the distances between query and index embeddings
  distances = np.linalg.norm(np.expand_dims(query_embeddings, 1) - np.expand_dims(index_embeddings, 0), axis=2)
  ranks = np.argsort(distances, axis=1)
  print('ranks computed!')

  index_idxs = ranks[idx][:];
  index_images = [index_list_new[i] for i in index_idxs];
  distance_images = [distances[idx][i] for i in index_idxs];
  
  for i in range(len(index_images)):
    print(index_images[i], distance_images[i]);
    
   
  with open("sorted_distances_imgs.pkl", "wb") as f:
    pickle.dump(query_img, f);
    pickle.dump(distance_images, f)
    pickle.dump(index_images, f)
    
    
  # ranks_before_gv = ranks

  # # Create output directory if necessary.
  # if not tf.io.gfile.exists(FLAGS.output_dir):
  #   tf.io.gfile.makedirs(FLAGS.output_dir)

  # # Compute metrics.
  # easy_metrics = ComputeMetrics(ranks_before_gv, easy_ground_truth,
  #                                       _PR_RANKS)
  # medium_metrics = ComputeMetrics(ranks_before_gv, medium_ground_truth,
  #                                         _PR_RANKS)
  # hard_metrics = ComputeMetrics(ranks_before_gv, hard_ground_truth,
  #                                       _PR_RANKS)

  # # Write metrics to file.
  # mean_average_precision_dict = {
  #     'easy': easy_metrics[0],
  #     'medium': medium_metrics[0],
  #     'hard': hard_metrics[0]
  # }
  # mean_precisions_dict = {'easy': easy_metrics[1], 'medium': medium_metrics[1], 'hard': hard_metrics[1]}
  # mean_recalls_dict = {'easy': easy_metrics[2], 'medium': medium_metrics[2], 'hard': hard_metrics[2]}
  # SaveMetricsFile(mean_average_precision_dict, mean_precisions_dict,
  #                         mean_recalls_dict, _PR_RANKS,
  #                         os.path.join(FLAGS.output_dir, _METRICS_FILENAME))


if __name__ == '__main__':
  app.run(main)