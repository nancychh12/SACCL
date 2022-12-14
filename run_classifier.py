# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
#
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
"""BERT finetuning runner."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import csv
import os
import logging
import argparse
import random
from tqdm import tqdm, trange

import numpy as np
import torch
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from torch.optim.lr_scheduler import CosineAnnealingLR

import tokenization
from modeling import BertConfig, BertForSequenceClassification
from optimization import BERTAdam

import json
import time
from sklearn.metrics import f1_score,precision_score,recall_score
from collections import Counter
from transformers import AutoConfig, AutoTokenizer
from modeling import AlbertForSequenceClassification

n_class = 4
reverse_order = False
sa_step = False

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',datefmt = '%m/%d/%Y %H:%M:%S',
                    filename='bert_empirical_CL.log',filemode='w',
                    level = logging.INFO)

console=logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)
logger = logging.getLogger(__name__)


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None, text_c=None):
        """Constructs a InputExample.

        Args:
            guid: Unique id for the example.
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.text_c = text_c
        self.label = label


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids, label_id):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id


class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_dev_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        with open(input_file, "r") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
            lines = []
            for line in reader:
                lines.append(line)
            return lines


class c3Processor(DataProcessor):
    def __init__(self):
        random.seed(42)
        self.D = [[], [], []]
        self.B = [[],[],[],[],[],[]]

        for sid in range(6):
            with open("../data/c3-train-sort-f"+str(sid+1)+".json", "r", encoding="utf8") as f:
                data = json.load(f)
            random.shuffle(data)
            for i in range(len(data)):
                for j in range(len(data[i][1])):
                    d = ['\n'.join(data[i][0]).lower(), data[i][1][j]["question"].lower()]
                    for k in range(len(data[i][1][j]["choice"])):
                        d += [data[i][1][j]["choice"][k].lower()]
                    for k in range(len(data[i][1][j]["choice"]), 4):
                        d += ['']
                    d += [data[i][1][j]["answer"].lower()]
                    self.B[sid] += [d]


        for sid in range(3):
            data = []
            for subtask in ["d", "m"]:
                with open("../data/c3-"+subtask+"-"+["train.json", "dev.json", "test.json"][sid], "r", encoding="utf8") as f:
                    data += json.load(f)

            if sid == 0:
                random.shuffle(data)

            for i in range(len(data)):
                for j in range(len(data[i][1])):
                    d = ['\n'.join(data[i][0]).lower(), data[i][1][j]["question"].lower()]
                    for k in range(len(data[i][1][j]["choice"])):
                        d += [data[i][1][j]["choice"][k].lower()]
                    for k in range(len(data[i][1][j]["choice"]), 4):
                        d += ['']
                    d += [data[i][1][j]["answer"].lower()] 
                    self.D[sid] += [d]
    
    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self.D[0], "train")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
                self.D[2], "test")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
                self.D[1], "dev")

    def get_bucket_examples(self,data_dir,num):
        return self._create_examples(
            self.B[num],"bucket"+str(num))

    def get_labels(self):
        """See base class."""
        return ["0", "1", "2", "3"]

    def _create_examples(self, data, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, d) in enumerate(data):
            for k in range(4):
                if data[i][2+k] == data[i][6]:
                    answer = str(k)
                    
            label = tokenization.convert_to_unicode(answer)

            for k in range(4):
                guid = "%s-%s-%s" % (set_type, i, k)
                text_a = tokenization.convert_to_unicode(data[i][0])
                text_b = tokenization.convert_to_unicode(data[i][k+2])
                text_c = tokenization.convert_to_unicode(data[i][1])
                examples.append(
                        InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label, text_c=text_c))
            
        return examples


def convert_examples_to_features(examples, label_list, max_seq_length, tokenizer):
    """Loads a data file into a list of `InputBatch`s."""

    print("#examples", len(examples))

    label_map = {}
    for (i, label) in enumerate(label_list):
        label_map[label] = i

    features = [[]]
    for (ex_index, example) in enumerate(examples):
        tokens_a = tokenizer.tokenize(example.text_a)

        tokens_b = tokenizer.tokenize(example.text_b)

        tokens_c = tokenizer.tokenize(example.text_c)

        _truncate_seq_tuple(tokens_a, tokens_b, tokens_c, max_seq_length - 4)

        tokens_b = tokens_c + ["[SEP]"] + tokens_b

        tokens = []
        segment_ids = []
        tokens.append("[CLS]")
        segment_ids.append(0)
        for token in tokens_a:
            tokens.append(token)
            segment_ids.append(0)
        tokens.append("[SEP]")
        segment_ids.append(0)

        if tokens_b:
            for token in tokens_b:
                tokens.append(token)
                segment_ids.append(1)
            tokens.append("[SEP]")
            segment_ids.append(1)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)   #tokens=CLS ?????? SEP ?????? SEP ??????

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        while len(input_ids) < max_seq_length:
            input_ids.append(0)
            input_mask.append(0)
            segment_ids.append(0)

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        label_id = label_map[example.label]
        if ex_index < 5:
            logger.info("*** Example ***")
            logger.info("guid: %s" % (example.guid))
            logger.info("tokens: %s" % " ".join(
                    [tokenization.printable_text(x) for x in tokens]))
            logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
            logger.info(
                    "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
            logger.info("label: %s (id = %d)" % (example.label, label_id))

        features[-1].append(
                InputFeatures(
                        input_ids=input_ids,
                        input_mask=input_mask,
                        segment_ids=segment_ids,
                        label_id=label_id))
        if len(features[-1]) == n_class:
            features.append([])

    if len(features[-1]) == 0:
        features = features[:-1]
    print('#features', len(features))
    return features



def _truncate_seq_tuple(tokens_a, tokens_b, tokens_c, max_length):
    """Truncates a sequence tuple in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence ???????????????????????????
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b) + len(tokens_c)
        if total_length <= max_length:
            break
        if len(tokens_a) >= len(tokens_b) and len(tokens_a) >= len(tokens_c):
            tokens_a.pop()
        elif len(tokens_b) >= len(tokens_a) and len(tokens_b) >= len(tokens_c):
            tokens_b.pop()
        else:
            tokens_c.pop()            


def accuracy(out, labels):
    outputs = np.argmax(out, axis=1)
    return np.sum(outputs==labels)

def F1(labels,logits_all):
    outputs=np.argmax(logits_all,axis=1)
    return f1_score(labels,outputs,average="macro")

def precision_recall_f1(labels,logits_all):
    """
    This function calculates and returns the precision, recall and f1-score
    Args:
        prediction: prediction string or list to be matched,????????????????????????
        ground_truth: golden string or list reference
    Returns:
        floats of (p, r, f1)
    Raises:
        None
    """

    outputs = np.argmax(logits_all, axis=1)
    p = precision_score(labels, outputs, average="macro")
    r = recall_score(labels, outputs, average="macro")
    f1 = f1_score(labels, outputs, average="macro")
    return p, r, f1

def feature2dataloader(bucket_features,batch_size):
    input_ids = []
    input_mask = []
    segment_ids = []
    label_id = []
    for f in bucket_features:
        input_ids.append([])
        input_mask.append([])
        segment_ids.append([])
        for i in range(n_class):
            input_ids[-1].append(f[i].input_ids)
            input_mask[-1].append(f[i].input_mask)
            segment_ids[-1].append(f[i].segment_ids)
        label_id.append([f[0].label_id])

    all_input_ids = torch.tensor(input_ids, dtype=torch.long)
    all_input_mask = torch.tensor(input_mask, dtype=torch.long)
    all_segment_ids = torch.tensor(segment_ids, dtype=torch.long)
    all_label_ids = torch.tensor(label_id, dtype=torch.long)

    bucket_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)

    bucket_sampler = RandomSampler(bucket_data)
    # train_sampler = SequentialSampler(train_data)

    bucket_dataloader = DataLoader(bucket_data, sampler=bucket_sampler, batch_size=batch_size)
    return bucket_dataloader

def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir",
                        default='../data',
                        type=str,
                        # required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--task_name",
                        default='c3',
                        type=str,
                        # required=True,
                        help="The name of the task to train.")
    parser.add_argument("--output_dir",
                        default="c3_curriculumLearning",
                        type=str,
                        # required=True,
                        help="The output directory where the model checkpoints will be written.")
    parser.add_argument("--bert_config_file",
                        default='../chinese_L-12_H-768_A-12/bert_config.json',
                        # default='../chinese_roberta_wwm_ext_pytorch/bert_config.json',
                        # default='../chinese_albert_base/config.json',
                        type=str,
                        # required=True,
                        help="The config json file corresponding to the pre-trained BERT model. \n"
                             "This specifies the model architecture.")
    parser.add_argument("--vocab_file",
                        default='../chinese_L-12_H-768_A-12/vocab.txt',
                        # default='../chinese_roberta_wwm_ext_pytorch/vocab.txt',
                        # default='../chinese_albert_base/vocab.txt',
                        type=str,
                        # required=True,
                        help="The vocabulary file that the BERT model was trained on.")

    ## Other parameters
    parser.add_argument("--init_checkpoint",
                        default='../chinese_L-12_H-768_A-12/pytorch_model.bin',
                        # default='../chinese_roberta_wwm_ext_pytorch/pytorch_model.bin',
                        # default='../chinese_albert_base/pytorch_model.bin',
                        type=str,
                        help="Initial checkpoint (usually from a pre-trained BERT model).")
    parser.add_argument("--model_name_or_path",
                        default='voidful/albert_chinese_base',
                        type=str,
                        help='Name of or path to the pretrained/trained model.For training choose between chinese-bert-base, chinese-albert-base etc. ')
    parser.add_argument("--max_seq_length",
                        default=512,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--do_train",
                        default=True,
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        default=True,
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_bucket",
                        default=True,
                        action='store_true',
                        help="Whether to run bucket")
    parser.add_argument("--train_batch_size",
                        default=24,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=6,
                        help="Number of updates steps to accumualte before performing a backward/update pass.")
    parser.add_argument("--learning_rate",
                        default=1e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=20.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--save_checkpoints_steps",
                        default=1000,
                        type=int,
                        help="How often to save the model checkpoint.")
    parser.add_argument("--no_cuda",
                        default=False,
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed', 
                        type=int, 
                        default=66,
                        help="random seed for initialization")
    parser.add_argument("--do_lower_case",
                        default=False,
                        action='store_true',
                        help="Whether to lower case the input text. True for uncased models, False for cased models.")
    parser.add_argument("--eval_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for eval.")

    args = parser.parse_args()
    logger.info(args)

    processors = {
        "c3": c3Processor,
    }

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
    logger.info("device %s n_gpu %d distributed training %r", device, n_gpu, bool(args.local_rank != -1))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            args.gradient_accumulation_steps))

    args.train_batch_size = int(args.train_batch_size / args.gradient_accumulation_steps)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_eval:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    bert_config = BertConfig.from_json_file(args.bert_config_file)
    # config = AutoConfig.from_pretrained(args.model_name_or_path)

    if args.max_seq_length > bert_config.max_position_embeddings:
        raise ValueError(
            "Cannot use sequence length {} because the BERT model was only trained up to sequence length {}".format(
            args.max_seq_length, bert_config.max_position_embeddings))

    # if os.path.exists(args.output_dir) and os.listdir(args.output_dir):
    #     if args.do_train:
    #         raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))
    # else:
    #     os.makedirs(args.output_dir, exist_ok=True)

    task_name = args.task_name.lower()

    if task_name not in processors:
        raise ValueError("Task not found: %s" % (task_name))

    processor = processors[task_name]()
    label_list = processor.get_labels()

    tokenizer = tokenization.FullTokenizer(vocab_file=args.vocab_file, do_lower_case=args.do_lower_case)
    # tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)

    train_examples = None
    num_train_steps = None
    if args.do_train:
        train_examples = processor.get_train_examples(args.data_dir)
        num_train_steps = int(
            len(train_examples) / n_class / args.train_batch_size / args.gradient_accumulation_steps * args.num_train_epochs)

    if args.do_bucket:
        bucket0_examples = processor.get_bucket_examples(args.data_dir, 0)
        bucket1_examples = processor.get_bucket_examples(args.data_dir, 1)
        bucket2_examples = processor.get_bucket_examples(args.data_dir, 2)
        bucket3_examples = processor.get_bucket_examples(args.data_dir, 3)
        bucket4_examples = processor.get_bucket_examples(args.data_dir, 4)
        bucket5_examples = processor.get_bucket_examples(args.data_dir, 5)

    model = BertForSequenceClassification(bert_config, 1 if n_class > 1 else len(label_list))
    # model = AlbertForSequenceClassification.from_pretrained(args.model_name_or_path, 1, config=config)

    if args.init_checkpoint is not None:
        # roberta
        # state_dict = torch.load(args.init_checkpoint, map_location='cpu')
        # old_keys = []
        # new_keys = []
        # for key in state_dict.keys():
        #     new_key = None
        #     if 'LayerNorm' in key:
        #         if 'weight' in key:
        #             new_key = key.replace('weight', 'gamma')
        #         if 'bias' in key:
        #             new_key = key.replace('bias', 'beta')
        #         if new_key:
        #             old_keys.append(key)
        #             new_keys.append(new_key)
        # for old_key, new_key in zip(old_keys, new_keys):
        #     state_dict[new_key] = state_dict.pop(old_key)
        # model.load_state_dict(state_dict,strict=False)

        #bert
        model.bert.load_state_dict(torch.load(args.init_checkpoint, map_location='cpu'))
        # checkpoint = torch.load(os.path.join(args.output_dir, "model_best.pt"),map_location='cpu')
        # model.load_state_dict(checkpoint['model'])
    model.to(device)

    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    no_decay = ['bias', 'gamma', 'beta']
    optimizer_parameters = [
        {'params': [p for n, p in model.named_parameters() if n not in no_decay], 'weight_decay_rate': 0.01},
        {'params': [p for n, p in model.named_parameters() if n in no_decay], 'weight_decay_rate': 0.0}
        ]

    optimizer = BERTAdam(optimizer_parameters,
                         lr=args.learning_rate,
                         warmup=args.warmup_proportion,
                         t_total=num_train_steps
                         )
    # scheduler = CosineAnnealingLR(optimizer, T_max=1798,eta_min=0)
    # optimizer.load_state_dict(checkpoint['optimizer'])

    global_step = 0

    if args.do_eval:
        eval_examples = processor.get_dev_examples(args.data_dir)
        eval_features = convert_examples_to_features(
            eval_examples, label_list, args.max_seq_length, tokenizer)

        input_ids = []
        input_mask = []
        segment_ids = []
        label_id = []
        
        for f in eval_features:
            input_ids.append([])
            input_mask.append([])
            segment_ids.append([])
            for i in range(n_class):
                input_ids[-1].append(f[i].input_ids)
                input_mask[-1].append(f[i].input_mask)
                segment_ids[-1].append(f[i].segment_ids)
            label_id.append([f[0].label_id])                

        all_input_ids = torch.tensor(input_ids, dtype=torch.long)
        all_input_mask = torch.tensor(input_mask, dtype=torch.long)
        all_segment_ids = torch.tensor(segment_ids, dtype=torch.long)
        all_label_ids = torch.tensor(label_id, dtype=torch.long)

        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
        if args.local_rank == -1:
            eval_sampler = SequentialSampler(eval_data)
        else:
            eval_sampler = DistributedSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

    if args.do_bucket:

        bucket0_features = convert_examples_to_features(bucket0_examples, label_list, args.max_seq_length, tokenizer)
        bucket1_features = convert_examples_to_features(bucket1_examples, label_list, args.max_seq_length, tokenizer)
        bucket2_features = convert_examples_to_features(bucket2_examples, label_list, args.max_seq_length, tokenizer)
        bucket3_features = convert_examples_to_features(bucket3_examples, label_list, args.max_seq_length, tokenizer)
        bucket4_features = convert_examples_to_features(bucket4_examples, label_list, args.max_seq_length, tokenizer)
        bucket5_features = convert_examples_to_features(bucket5_examples, label_list, args.max_seq_length, tokenizer)

        bucket1_features.extend(bucket0_features)
        bucket2_features.extend(bucket1_features)
        bucket3_features.extend(bucket2_features)
        bucket4_features.extend(bucket3_features)
        bucket5_features.extend(bucket4_features)

        bucket0_dataloader = feature2dataloader(bucket0_features, args.train_batch_size)
        bucket1_dataloader = feature2dataloader(bucket1_features, args.train_batch_size)
        bucket2_dataloader = feature2dataloader(bucket2_features, args.train_batch_size)
        bucket3_dataloader = feature2dataloader(bucket3_features, args.train_batch_size)
        bucket4_dataloader = feature2dataloader(bucket4_features, args.train_batch_size)
        bucket5_dataloader = feature2dataloader(bucket5_features, args.train_batch_size)

        logger.info("len_bucket0_dataloader=%d" % len(bucket0_dataloader))
        logger.info("len_bucket1_dataloader=%d" % len(bucket1_dataloader))
        logger.info("len_bucket2_dataloader=%d" % len(bucket2_dataloader))
        logger.info("len_bucket3_dataloader=%d" % len(bucket3_dataloader))
        logger.info("len_bucket4_dataloader=%d" % len(bucket4_dataloader))
        logger.info("len_bucket5_dataloader=%d" % len(bucket5_dataloader))


        logger.info("***** Running training with bucket*****")
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_steps)
        all_loaders = [bucket0_dataloader, bucket1_dataloader, bucket2_dataloader,
                       bucket3_dataloader,
                       bucket4_dataloader,
                       bucket5_dataloader]

        best_accuracy = 0
        increase=True
        # j=-1
        for _epoch in range(13):
            j= _epoch //2   # j = _epoch
            if j > 5:
                j = 5
            # if increase:
            #     j += 1
            #     if j == 5:
            #         increase = False
            #         # j=5
            # else:
            #     j -= 1
            #     if j == 0:
            #         increase = True
            #         # j=0

            model.train()
            tr_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            start_time = time.time()
            elapsed_time=0
            for step, batch in enumerate(tqdm(all_loaders[j] , desc="bucket_Iteration")):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids = batch
                loss, _ = model(input_ids, segment_ids, input_mask, label_ids, n_class)
                if n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                loss.backward()
                tr_loss += loss.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    optimizer.step()  # We have accumulated enought gradients
                    model.zero_grad()
                    global_step += 1
                    # scheduler.step()
                # if (step + 1) % (len(all_loaders[j]) // 4) == 0:
                #     elapsed_time += (time.time() - start_time)


            logger.info("???%d???epoch????????????:%s" % (_epoch, str(optimizer.get_lr()[0])))
            # logger.info("???%d???epoch????????????:%f" % (_epoch, optimizer.param_groups[0]['lr']))

            logger.info("bucket_epoch=%d,step=%d" %(_epoch,step))
            elapsed_time =( time.time() - start_time)
            logger.info("bucket_epoch=%d, elpased_time=%d(?????????????????????)" % (_epoch, elapsed_time))

            model.eval()
            eval_loss, eval_accuracy = 0, 0
            nb_eval_steps, nb_eval_examples = 0, 0
            logits_all = []
            label_ids_all = []
            for input_ids, input_mask, segment_ids, label_ids in eval_dataloader:
                input_ids = input_ids.to(device)
                input_mask = input_mask.to(device)
                segment_ids = segment_ids.to(device)
                label_ids = label_ids.to(device)

                with torch.no_grad():
                    tmp_eval_loss, logits = model(input_ids, segment_ids, input_mask, label_ids, n_class)

                logits = logits.detach().cpu().numpy()
                label_ids = label_ids.to('cpu').numpy()
                for i in range(len(logits)):
                    logits_all += [logits[i]]
                for i in range(len(label_ids)):
                    label_ids_all += [label_ids[i]]

                tmp_eval_accuracy = accuracy(logits, label_ids.reshape(-1))

                eval_loss += tmp_eval_loss.mean().item()
                eval_accuracy += tmp_eval_accuracy

                nb_eval_examples += input_ids.size(0)
                nb_eval_steps += 1

            eval_loss = eval_loss / nb_eval_steps
            eval_accuracy = eval_accuracy / nb_eval_examples
            pre, rec, f1 = precision_recall_f1(label_ids_all, logits_all)
            if args.do_train:
                result = {'eval_loss': eval_loss,
                          'eval_accuracy': eval_accuracy,
                          'global_step': global_step,
                          'loss': tr_loss / nb_tr_steps,
                          'f1': f1,
                          'pre': pre,
                          'rec': rec}
            else:
                result = {'eval_loss': eval_loss,
                          'eval_accuracy': eval_accuracy,
                          'f1': f1,
                          'pre': pre,
                          'rec': rec}

            logger.info("***** ???%d???epoch??????*??? Eval results *****" % (_epoch))
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))

            if eval_accuracy >= best_accuracy:
                state = {"model": model.state_dict(), "optimizer": optimizer.state_dict(), "epoch": _epoch}
                torch.save(state, os.path.join(args.output_dir, "model_best.pt"))
                best_accuracy = eval_accuracy
            # start_time = time.time()



    checkpoint = torch.load(os.path.join(args.output_dir, "model_best.pt"))
    model.load_state_dict(checkpoint['model'])
    optimizer.load_state_dict(checkpoint['optimizer'])
    epoch = checkpoint['epoch']
    # model.load_state_dict(torch.load(os.path.join(args.output_dir, "model.pt")))

    if args.do_eval:
        #?????????dev.json
        logger.info("***** Running evaluation *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)

        model.eval()
        eval_loss, eval_accuracy = 0, 0
        nb_eval_steps, nb_eval_examples = 0, 0
        logits_all = []
        label_ids_all=[]
        for input_ids, input_mask, segment_ids, label_ids in eval_dataloader:
            input_ids = input_ids.to(device)
            input_mask = input_mask.to(device)
            segment_ids = segment_ids.to(device)
            label_ids = label_ids.to(device)

            with torch.no_grad():
                tmp_eval_loss, logits = model(input_ids, segment_ids, input_mask, label_ids, n_class)

            logits = logits.detach().cpu().numpy()
            label_ids = label_ids.to('cpu').numpy()
            for i in range(len(logits)):
                logits_all += [logits[i]]

            for i in range(len(label_ids)):
                label_ids_all +=[label_ids[i]]

            tmp_eval_accuracy = accuracy(logits, label_ids.reshape(-1))

            eval_loss += tmp_eval_loss.mean().item()
            eval_accuracy += tmp_eval_accuracy

            nb_eval_examples += input_ids.size(0)
            nb_eval_steps += 1

        eval_loss = eval_loss / nb_eval_steps
        eval_accuracy = eval_accuracy / nb_eval_examples

        # f1=F1(label_ids_all,logits_all)
        pre, rec, f1 = precision_recall_f1(label_ids_all, logits_all)
        if args.do_train:
            result = {'eval_loss': eval_loss,
                      'eval_accuracy': eval_accuracy,
                      'global_step': global_step,
                      'loss': tr_loss/nb_tr_steps,
                      'f1':f1,
                      'pre':pre,
                      'rec':rec}
        else:
            result = {'eval_loss': eval_loss,
                      'eval_accuracy': eval_accuracy,
                      'f1':f1,
                      'pre':pre,
                      'rec':rec}


        output_eval_file = os.path.join(args.output_dir, "eval_results_dev.txt")
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))
        output_eval_file = os.path.join(args.output_dir, "logits_dev.txt")
        with open(output_eval_file, "w") as f:
            for i in range(len(logits_all)):
                for j in range(len(logits_all[i])):
                    f.write(str(logits_all[i][j]))
                    if j == len(logits_all[i])-1:
                        f.write("\n")
                    else:
                        f.write(" ")

        #?????????test.json
        eval_examples = processor.get_test_examples(args.data_dir)
        eval_features = convert_examples_to_features(
            eval_examples, label_list, args.max_seq_length, tokenizer)

        logger.info("***** Running evaluation *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)

        input_ids = []
        input_mask = []
        segment_ids = []
        label_id = []
        
        for f in eval_features:
            input_ids.append([])
            input_mask.append([])
            segment_ids.append([])
            for i in range(n_class):
                input_ids[-1].append(f[i].input_ids)
                input_mask[-1].append(f[i].input_mask)
                segment_ids[-1].append(f[i].segment_ids)
            label_id.append([f[0].label_id])                

        all_input_ids = torch.tensor(input_ids, dtype=torch.long)
        all_input_mask = torch.tensor(input_mask, dtype=torch.long)
        all_segment_ids = torch.tensor(segment_ids, dtype=torch.long)
        all_label_ids = torch.tensor(label_id, dtype=torch.long)

        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
        if args.local_rank == -1:
            eval_sampler = SequentialSampler(eval_data)
        else:
            eval_sampler = DistributedSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

        model.eval()
        eval_loss, eval_accuracy = 0, 0
        nb_eval_steps, nb_eval_examples = 0, 0
        logits_all = []
        label_ids_all=[]
        for input_ids, input_mask, segment_ids, label_ids in eval_dataloader:
            input_ids = input_ids.to(device)
            input_mask = input_mask.to(device)
            segment_ids = segment_ids.to(device)
            label_ids = label_ids.to(device)

            with torch.no_grad():
                tmp_eval_loss, logits = model(input_ids, segment_ids, input_mask, label_ids, n_class)

            logits = logits.detach().cpu().numpy()
            label_ids = label_ids.to('cpu').numpy()
            for i in range(len(logits)):
                logits_all += [logits[i]]
            for i in range(len(label_ids)):
                label_ids_all += [label_ids[i]]

            tmp_eval_accuracy = accuracy(logits, label_ids.reshape(-1))

            eval_loss += tmp_eval_loss.mean().item()
            eval_accuracy += tmp_eval_accuracy

            nb_eval_examples += input_ids.size(0)
            nb_eval_steps += 1

        eval_loss = eval_loss / nb_eval_steps
        eval_accuracy = eval_accuracy / nb_eval_examples
        # f1 = F1(label_ids_all, logits_all)
        pre, rec, f1 = precision_recall_f1(label_ids_all, logits_all)
        if args.do_train:
            result = {'eval_loss': eval_loss,
                      'eval_accuracy': eval_accuracy,
                      'global_step': global_step,
                      'loss': tr_loss/nb_tr_steps,
                      'f1':f1,
                      'pre':pre,
                      'rec':rec}
        else:
            result = {'eval_loss': eval_loss,
                      'eval_accuracy': eval_accuracy,
                      'f1':f1,
                      'pre':pre,
                      'rec':rec}


        output_eval_file = os.path.join(args.output_dir, "eval_results_test.txt")
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))
        output_eval_file = os.path.join(args.output_dir, "logits_test.txt")
        with open(output_eval_file, "w") as f:
            for i in range(len(logits_all)):
                for j in range(len(logits_all[i])):
                    f.write(str(logits_all[i][j]))
                    if j == len(logits_all[i])-1:
                        f.write("\n")
                    else:
                        f.write(" ")

if __name__ == "__main__":
    main()
