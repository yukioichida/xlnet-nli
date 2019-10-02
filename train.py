#!/usr/bin/env python3

import torch
import numpy as np
import argparse
from pytorch_transformers import XLNetForSequenceClassification, XLNetConfig, XLNetTokenizer
from modules.preprocess import MNLIDatasetReader
from torch.utils.data import DataLoader, RandomSampler
from pytorch_transformers import AdamW, WarmupLinearSchedule
from tqdm import tqdm, trange

from modules.log import get_logger

LOG = get_logger('train_xlnet')


def train_configs(max_seq_len, tokenizer, device):
    base_path = '/home/ichida/dev_env/ml/data/multinli_1.0'
    train_file = 'multinli_1.0_train_reduced_40samples.csv'
    val_file = 'multinli_1.0_dev_matched_reduced.txt'
    return {
        'train_file': f'{base_path}/{train_file}',
        'val_file': f'{base_path}/{val_file}',
        'max_seq_len': max_seq_len,
        'tokenizer': tokenizer,
        'device': device
    }


def get_train_dataset_loader(max_seq_len, tokenizer, device, batch_size):
    paths = train_configs(max_seq_len, tokenizer, device)
    reader = MNLIDatasetReader(**paths)
    train_dataset = reader.load_train_dataset()
    return DataLoader(train_dataset, batch_size, RandomSampler(train_dataset))


def get_val_dataset_loader(max_seq_len, tokenizer, device, batch_size):
    paths = train_configs(max_seq_len, tokenizer, device)
    reader = MNLIDatasetReader(**paths)
    val_dataset = reader.load_val_dataset()
    return DataLoader(val_dataset, batch_size, RandomSampler(val_dataset))


def train(args, device):
    model_name = 'xlnet-base-cased'
    LOG.info(f'Setup {model_name} tokenizer...')
    tokenizer = XLNetTokenizer.from_pretrained(model_name, do_lower_case=True)
    LOG.info(f'Setup model {model_name}...')
    model_name = 'xlnet-base-cased'
    xlnet_config = XLNetConfig.from_pretrained(model_name,
                                               output_hidden_states=True,
                                               output_attentions=True,
                                               num_labels=3,
                                               finetuning_task='NLI')

    model = XLNetForSequenceClassification.from_pretrained(model_name, config=xlnet_config)
    model.to(device)
    train_dataloader = get_train_dataset_loader(args.max_seq_len, tokenizer, device, args.batch_size)

    LOG.info("Setup Optimizer and Loss Function")
    # Prepare optimizer and schedule (linear warmup and decay)
    t_total = len(train_dataloader) // args.epochs
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
         'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
         'weight_decay': 0.0}
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = WarmupLinearSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=t_total)

    LOG.info("Training Started!")

    model.zero_grad()
    for epoch in trange(args.epochs, desc="Epoch"):
        executed_steps = 0
        train_epoch_iterator = tqdm(train_dataloader, desc="Iteration")
        for step, batch in enumerate(train_epoch_iterator):
            model.train()
            model_input = {'input_ids': batch[0],  # word ids
                           'attention_mask': batch[1],  # input mask
                           'token_type_ids': batch[2],  # segment ids
                           'labels': batch[3]}  # labels

            outputs = model(**model_input)
            train_loss, predictions = outputs[0:2]

            train_loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_norm)  # grad clip
            optimizer.step()
            scheduler.step()
            model.zero_grad()
            executed_steps += 1

        evaluation(epoch=epoch, model=model, tokenizer=tokenizer, args=args, device=device)
        train_epoch_iterator.close()


def evaluation(epoch, model, tokenizer, args, device):
    val_dataloader = get_val_dataset_loader(args.max_seq_len, tokenizer, device, args.batch_size)
    epoch_val_loss = 0.0
    executed_steps = 0
    preds = None
    for batch in tqdm(val_dataloader, desc="Evaluation Step for epoch {}".format(epoch)):
        model.eval()
        with torch.no_grad():
            model_input = {'input_ids': batch[0],  # word ids
                           'attention_mask': batch[1],  # input mask
                           'token_type_ids': batch[2],  # segment ids
                           'labels': batch[3]}  # labels

            outputs = model(**model_input)
            val_loss, val_logits = outputs[0:2]
            epoch_val_loss += val_loss.mean().item()
            if preds is None:
                preds = val_logits.detach().cpu().numpy()
                labels = model_input['labels'].detach().cpu().numpy()
            else:
                preds = np.append(preds, val_logits.detach().cpu().numpy(), axis=0)
                labels = np.append(labels, model_input['labels'].detach().cpu().numpy(), axis=0)
        executed_steps += 1

    epoch_val_loss = epoch_val_loss / executed_steps
    preds = np.argmax(preds, axis=1)
    acc = (preds == labels).mean()
    LOG.info(f'Epoch {epoch} - Val:[loss = {epoch_val_loss}, acc = {acc}]')


if __name__ == '__main__':
    argparser = argparse.ArgumentParser()

    argparser.add_argument('--batch_size', type=int, default=8)
    argparser.add_argument('--clip_norm', type=float, default=1.0, help="Gradient clipping parameter")
    argparser.add_argument('--epochs', type=int, default=10, help="Train epochs")
    argparser.add_argument('--max_seq_len', type=int, default=128, help="Max Sequence Length")

    argparser.add_argument('--learning_rate', type=float, default=3e-5)
    argparser.add_argument('--adam_epsilon', type=float, default=1e-8)
    argparser.add_argument('--weight_decay', type=float, default=0.0)
    argparser.add_argument('--warmup_steps', type=int, default=2000)
    args = argparser.parse_args()

    tensor_device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    train(args, tensor_device)
