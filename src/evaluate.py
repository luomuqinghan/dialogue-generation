"""
@author:    Patrik Purgai
@copyright: Copyright 2019, dialogue-generation
@license:   MIT
@email:     purgai.patrik@gmail.com
@date:      2019.04.04.
"""

# pylint: disable=import-error
# pylint: disable=no-member
# pylint: disable=no-name-in-module

import sys
import json
import tempfile
import argparse
import torch
import subprocess

from tqdm import tqdm
from tabulate import tabulate
from math import ceil

from os.path import (
    join, abspath, dirname)

# HACK to enable launching with
# python src/train.py
PROJECT_PATH = join(abspath(dirname(__file__)), '..')
if PROJECT_PATH not in sys.path:
    sys.path.append(PROJECT_PATH)

from src.data import (
    setup_data_args,
    create_dataset,
    transform_dialog,
    RSP, SP1, SP2, HST)

from src.model import (
    create_model,
    setup_model_args)

from src.train import set_random_seed

from src.collate import PREPARE

from src.interact import (
    decode, 
    select_nucleus, 
    select_topk,
    setup_eval_args,
    METHODS)


def main():
    args = setup_eval_args()

    args.batch_size = 1
    args.min_len = 2

    # evaluation mode only processes a single element
    args.distributed = False

    args.cuda = not args.no_cuda and \
        torch.cuda.is_available()

    if args.seed is not None:
        set_random_seed(args)

    device = torch.device(
        'cuda' if args.cuda else 'cpu')
    
    assert args.name is not None, \
        '`--name` must be given'

    model_dir = join(
        args.model_dir, args.model, args.name)

    model_path = args.model_file if \
        args.model_file else \
        join(model_dir, args.ckpt_name + '.pt')
    
    state_dict = torch.load(
        model_path, map_location=device)

    del state_dict['optimizer']

    datasets, tokenizer, max_len = create_dataset(
        args, master_process=True)

    vocab_size = len(tokenizer)

    model = create_model(args, model_dir, vocab_size)
    model = model.to(device)
    
    try:
        model.load_state_dict(state_dict.pop('model'))
        model.eval()
    except RuntimeError as e:
        print(
            'The provided checkpoint has mismatching '
            'weights in the parameter dict.')

        print(
            'WARNING: If the model was trained with '
            '`--grad_ckpt` you also have to provide '
            'this argument for this script.')

        sys.exit()

    print()
    print(tabulate(state_dict.items(), tablefmt='presto'))
    print()

    history = []

    select_fn = METHODS[args.decoding]

    @torch.no_grad()
    def predict(batch):
        """
        Responds to the given text.
        """
        inputs, _ = batch

        input_ids, type_ids = inputs
        inputs = (input_ids.tolist(), type_ids.tolist())

        preds = decode(
            args=args, model=model,
            inputs=inputs, tokenizer=tokenizer,
            select_fn=select_fn, device=device)
        
        preds = [
            tokenizer.decode(p, True) 
            for p in preds
        ]

        return preds

    def normalize_input(inputs):
        """
        Removes the special speaker tokens from
        the input text.
        """
        split = inputs.split('<sp1>')
        inputs, targets = split[:-1], split[-1]
        inputs = ' '.join(' '.join(inputs).split('<sp2>'))
        inputs = inputs.strip()

        return inputs, targets

    train, _, test = [
        (split, ceil(
            size / args.batch_size))
        for split, size in datasets]

    # computing the sizes of the dataset splits
    train_dataset, num_train_steps = train
    test_dataset, num_test_steps = test

    vocab = set()

    with tempfile.NamedTemporaryFile('w') as tns, \
            tempfile.NamedTemporaryFile('w') as tts, \
            tempfile.NamedTemporaryFile('w') as ttt, \
            tempfile.NamedTemporaryFile('w') as r, \
            tempfile.NamedTemporaryFile('w') as tv:
        # opening the resulting files as temporaries
        # saving training examples and predictions

        loop = tqdm(
            train_dataset(), 
            desc='processing train',
            leave=False,
            total=num_train_steps)

        # # processing the training data and building a
        # # vocabulary for the evaluation script
        for batch in loop:
            for inputs in batch[0][0].tolist():
                inputs = tokenizer.decode(inputs, True)
                inp, _ = normalize_input(inputs)

                for word in inp.lower().split():
                    vocab.add(word)

                tns.write(inp.lower() + '\n')

        loop = tqdm(
            test_dataset(), 
            desc='processing test',
            leave=False,
            total=num_test_steps)

        # processing test data and making predictions
        # with the provided decoding method
        for batch in loop:
            inputs = batch[0][0].tolist()
                
            preds = predict(batch)

            for inp, prd in zip(inputs, preds):
                inp = tokenizer.decode(inp, True)

                # removing speaker tokens from the input
                # and creating target sentence
                inp, trg = normalize_input(inp)
                
                tts.write(inp.lower() + '\n')
                r.write(prd.lower() + '\n')
                ttt.write(trg.lower() + '\n')

                for word in inp.lower().split():
                    vocab.add(word)

                for word in trg.lower().split():
                    vocab.add(word)

        tv.write('\n'.join(vocab))

        tns.flush()
        tts.flush()
        ttt.flush()
        tv.flush()
        r.flush()

        script_path = join(
            PROJECT_PATH, 'dialog-eval', 'code', 'main.py')

        command = f'python {script_path} ' + \
            f'-tns={tns.name} -tts={tts.name} ' + \
            f'-ttt={ttt.name} -r={r.name} ' + \
            f'-tv={tv.name}'

        print('Running evaluation script ...')

        process = subprocess.Popen(
            command.split(), 
            stdin=subprocess.PIPE, 
            stdout=subprocess.PIPE)

        outs, errs = process.communicate(
            'y'.encode(), timeout=120)

    output_path = join(
        abspath(dirname(tns.name)), 'metrics.txt')

    metric_path = join(model_dir, 'metric.json')
    
    with open('/tmp/metrics.txt', 'r') as fh:
        metrics = fh.readline().split()[1:]
        
        # loading the output metrics organized into
        # columns of mean, std and confidence
        values = [
            [m] + v.split(',') for m, v in 
            zip(metrics, fh.readline().split()[1:])
        ]

    columns = list(zip(*values))

    headers = ['metrics', 'mean', 'std', 'confidence']

    with open(metric_path, 'w') as fh:
        json.dump({
            h: c for h, c in 
            zip(headers, columns)
        }, fh)

    print(tabulate(values, headers=headers, floatfmt='.4f'))


if __name__ == '__main__':
    main()