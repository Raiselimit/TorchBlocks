import os
import csv
from torchblocks.metrics import MattewsCorrcoef
from torchblocks.callback import TrainLogger
from torchblocks.trainer import TextClassifierTrainer
from torchblocks.processor import TextClassifierProcessor, InputExample
from torchblocks.utils import seed_everything, dict_to_text, build_argparse
from torchblocks.utils import prepare_device, get_checkpoints
from torchblocks.optim import Lookahead, AdamW
from transformers import BertForSequenceClassification, BertConfig, BertTokenizer
from transformers import WEIGHTS_NAME

MODEL_CLASSES = {
    'bert': (BertConfig, BertForSequenceClassification, BertTokenizer)
}

'''
Lookahead Optimizer: k steps forward, 1 step back
'''


class ColaProcessor(TextClassifierProcessor):
    def __init__(self, tokenizer, data_dir, prefix):
        super().__init__(tokenizer=tokenizer, data_dir=data_dir, prefix=prefix)

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def read_data(self, input_file):
        """Reads a json list file."""
        with open(input_file, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=None)
            lines = []
            for line in reader:
                lines.append(line)
            return lines

    def create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            guid = "%s-%s" % (set_type, i)
            text_a = line[3] if set_type != 'test' else line[1]
            label = line[1] if set_type != 'test' else None
            examples.append(
                InputExample(guid=guid, texts=[text_a, None], label=label))
        return examples


class LookaheadTrainer(TextClassifierTrainer):
    def __init__(self, args, metrics, logger, batch_input_keys, collate_fn=None):
        super().__init__(args=args, metrics=metrics, logger=logger,
                         batch_input_keys=batch_input_keys,
                         collate_fn=collate_fn)

    def build_optimizers(self, model):
        '''
        Setup the optimizer and the learning rate scheduler.
        '''
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {"params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
             "weight_decay": self.args.weight_decay},
            {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
             "weight_decay": 0.0},
        ]
        base_optimizer = AdamW(optimizer_grouped_parameters, lr=self.args.learning_rate, eps=self.args.adam_epsilon)
        optimizer = Lookahead(base_optimizer, k=self.args.look_k, alpha=self.args.look_alpha)
        return optimizer


def main():
    parser = build_argparse()
    parser.add_argument('--look_k', type=int, default=5)
    parser.add_argument('--look_alpha', type=float, default=0.5)
    args = parser.parse_args()
    if args.model_name is None:
        args.model_name = args.model_path.split("/")[-1]
    args.output_dir = args.output_dir + '{}'.format(args.model_name)
    os.makedirs(args.output_dir, exist_ok=True)

    prefix = "_".join([args.model_name, args.task_name])
    logger = TrainLogger(log_dir=args.output_dir, prefix=prefix)

    logger.info("initializing device")
    args.device, args.n_gpu = prepare_device(args.gpu, args.local_rank)
    seed_everything(args.seed)

    args.model_type = args.model_type.lower()
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]

    logger.info("initializing data processor")
    tokenizer = tokenizer_class.from_pretrained(args.model_path, do_lower_case=args.do_lower_case)
    processor = ColaProcessor(tokenizer, args.data_dir, prefix=prefix)
    label_list = processor.get_labels()
    num_labels = len(label_list)
    args.num_labels = num_labels

    logger.info("initializing model and config")
    config = config_class.from_pretrained(args.model_path, num_labels=num_labels,
                                          cache_dir=args.cache_dir if args.cache_dir else None)
    model = model_class.from_pretrained(args.model_path, config=config)
    model.to(args.device)

    logger.info("initializing traniner")
    trainer = LookaheadTrainer(logger=logger, args=args,
                               batch_input_keys=processor.get_batch_keys(),
                               collate_fn=processor.collate_fn,
                               metrics=[MattewsCorrcoef()])
    if args.do_train:
        train_dataset = processor.create_dataset(max_seq_length=args.train_max_seq_length,
                                                 data_name='train.tsv', mode='train')
        eval_dataset = processor.create_dataset(max_seq_length=args.eval_max_seq_length,
                                                data_name='dev.tsv', mode='dev')
        trainer.train(model, train_dataset=train_dataset, eval_dataset=eval_dataset)

    if args.do_eval and args.local_rank in [-1, 0]:
        results = {}
        eval_dataset = processor.create_dataset(max_seq_length=args.eval_max_seq_length,
                                                data_name='dev.tsv', mode='dev')
        checkpoints = [args.output_dir]
        if args.eval_all_checkpoints or args.checkpoint_number > 0:
            checkpoints = get_checkpoints(args.output_dir, args.checkpoint_number, WEIGHTS_NAME)
        logger.info("Evaluate the following checkpoints: %s", checkpoints)
        for checkpoint in checkpoints:
            global_step = checkpoint.split("/")[-1].split("-")[-1]
            model = model_class.from_pretrained(checkpoint, config=config)
            model.to(args.device)
            trainer.evaluate(model, eval_dataset, save_preds=True, prefix=str(global_step))
            if global_step:
                result = {"{}_{}".format(global_step, k): v for k, v in trainer.records['result'].items()}
                results.update(result)
        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        dict_to_text(output_eval_file, results)

    if args.do_predict:
        test_dataset = processor.create_dataset(max_seq_length=args.eval_max_seq_length,
                                                data_name='test.tsv', mode='test')
        if args.checkpoint_number == 0:
            raise ValueError("checkpoint number should > 0,but get %d", args.checkpoint_number)
        checkpoints = get_checkpoints(args.output_dir, args.checkpoint_number, WEIGHTS_NAME)
        for checkpoint in checkpoints:
            global_step = checkpoint.split("/")[-1].split("-")[-1]
            model = model_class.from_pretrained(checkpoint)
            model.to(args.device)
            trainer.predict(model, test_dataset=test_dataset, prefix=str(global_step))


if __name__ == "__main__":
    main()