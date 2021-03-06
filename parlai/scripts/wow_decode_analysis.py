#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""
Basic example which iterates through the tasks specified and evaluates the given model
on them.

## Examples

```shell
parlai eval_model --task "babi:Task1k:2" -m "repeat_label"
parlai eval_model --task convai2 --model-file "/path/to/model_file"
```
"""

from parlai.core.params import ParlaiParser, print_announcements
from parlai.core.agents import create_agent
from parlai.core.logs import TensorboardLogger
from parlai.core.metrics import (
    aggregate_named_reports,
    aggregate_unnamed_reports,
    Metric,
)
from parlai.core.worlds import create_task
from parlai.utils.misc import TimeLogger, nice_report
from parlai.utils.world_logging import WorldLogger
from parlai.core.script import ParlaiScript, register_script
from parlai.utils.io import PathManager
import parlai.utils.logging as logging

import json
import os
import random

from parlai.utils.distributed import (
    is_primary_worker,
    all_gather_list,
    is_distributed,
    get_rank,
)


def setup_args(parser=None):
    if parser is None:
        parser = ParlaiParser(True, True, 'Evaluate a model')
    # Get command line arguments
    parser.add_argument(
        '-rf',
        '--report-filename',
        type=str,
        default='',
        help='Saves a json file of the evaluation report either as an '
        'extension to the model-file (if begins with a ".") or a whole '
        'file path. Set to the empty string to not save at all.',
    )
    parser.add_argument(
        '--world-logs',
        type=str,
        default='',
        help='Saves a jsonl file of the world logs.'
        'Set to the empty string to not save at all.',
    )
    parser.add_argument(
        '--save-format',
        type=str,
        default='conversations',
        choices=['conversations', 'parlai'],
    )
    parser.add_argument('-ne', '--num-examples', type=int, default=-1)
    parser.add_argument('-d', '--display-examples', type='bool', default=False)
    parser.add_argument('-ltim', '--log-every-n-secs', type=float, default=10)
    parser.add_argument(
        '-mcs',
        '--metrics',
        type=str,
        default='default',
        help='list of metrics to show/compute, e.g. all, default,'
        'or give a list split by , like '
        'ppl,f1,accuracy,hits@1,rouge,bleu'
        'the rouge metrics will be computed as rouge-1, rouge-2 and rouge-l',
    )
    parser.add_argument(
        '-micro',
        '--aggregate-micro',
        type='bool',
        default=False,
        help='Report micro-averaged metrics instead of macro averaged metrics.',
        recommended=False,
    )
    WorldLogger.add_cmdline_args(parser, partial_opt=None)
    TensorboardLogger.add_cmdline_args(parser, partial_opt=None)
    parser.set_params(datatype='valid')
    return parser


def _save_eval_stats(opt, report):
    if not is_primary_worker:
        return
    report_fname = opt['report_filename']
    if report_fname == '':
        return
    if report_fname.startswith('.'):
        report_fname = opt['model_file'] + report_fname

    json_serializable_report = report
    for k, v in report.items():
        if isinstance(v, Metric):
            v = v.value()
        json_serializable_report[k] = v

    # Save report
    with PathManager.open(report_fname, 'w') as f:
        logging.info(f'Saving model report to {report_fname}')
        json.dump({'opt': opt, 'report': json_serializable_report}, f, indent=4)
        f.write("\n")  # for jq


def _eval_single_world(opt, agent, task):
    logging.info(f'Evaluating task {task} using datatype {opt.get("datatype")}.')
    # set up world logger
    world_logger = WorldLogger(opt) if opt['world_logs'] else None

    task_opt = opt.copy()  # copy opt since we're editing the task
    task_opt['task'] = task
    world = create_task(task_opt, agent)  # create worlds for tasks

    # set up logging
    log_every_n_secs = opt.get('log_every_n_secs', -1)
    if log_every_n_secs <= 0:
        log_every_n_secs = float('inf')
    log_time = TimeLogger()

    # max number of examples to evaluate
    max_cnt = opt['num_examples'] if opt['num_examples'] > 0 else float('inf')
    cnt = 0
    total_cnt = world.num_examples()

    if is_distributed():
        logging.warning('Progress bar is approximate in distributed mode.')
    # save csv file
    import pandas as pd
    
    list_topic = []
    list_last_sentence = []
    list_checked_sentence = []
    list_eval_label = []
    list_generated_sentence = []
    while not world.epoch_done() and cnt < max_cnt:
        cnt += opt.get('batchsize', 1)
        world.parley()
        if world_logger is not None:
            world_logger.log(world)
        if opt['display_examples']:
            
            # display examples
            acts, responses = world.get_acts()
            # print(len(responses))
            # print(world.display() + '\n~~')
            # print(len(acts))
            for idx in range(len(acts)):
                act = acts[idx]
                response = responses[idx]
                topic = act.get('title', None)
                last_sentence = act.get('text', None)
                checked_sentence = act.get('checked_sentence', None)
                eval_sentences = act.get('eval_labels', [])
                eval_sentence = None
                if len(eval_sentences) > 0:
                    eval_sentence = eval_sentences[0]
                generated_sentence = response.get('text', None)
                

                # print(topic)
                # print(checked_sentence)
                # print(eval_sentence)
                # print(generated_sentence)

                list_topic.append(topic)
                list_last_sentence.append(last_sentence)
                list_checked_sentence.append(checked_sentence)
                list_eval_label.append(eval_sentence)
                list_generated_sentence.append(generated_sentence)

        if log_time.time() > log_every_n_secs:
            report = world.report()
            text, report = log_time.log(
                report.get('exs', 0), min(max_cnt, total_cnt), report
            )
            logging.info(text)
    save_data = {'topic': list_topic, 'last_sentence':list_last_sentence, 'checked_sentence': list_checked_sentence, 'eval_label':list_eval_label, 'generated_sentence':list_generated_sentence}
    saved_df = pd.DataFrame(save_data)
    saved_df.to_csv('test.csv')

    if world_logger is not None:
        # dump world acts to file
        world_logger.reset()  # add final acts to logs
        if is_distributed():
            rank = get_rank()
            base_outfile, extension = os.path.splitext(opt['world_logs'])
            outfile = base_outfile + f'_{rank}' + extension
        else:
            outfile = opt['world_logs']
        world_logger.write(outfile, world, file_format=opt['save_format'])

    report = aggregate_unnamed_reports(all_gather_list(world.report()))
    world.reset()

    return report


def eval_model(opt):
    """
    Evaluates a model.

    :param opt: tells the evaluation function how to run
    :return: the final result of calling report()
    """
    random.seed(42)
    if 'train' in opt['datatype'] and 'evalmode' not in opt['datatype']:
        raise ValueError(
            'You should use --datatype train:evalmode if you want to evaluate on '
            'the training set.'
        )

    # load model and possibly print opt
    agent = create_agent(opt, requireModelExists=True)
    agent.opt.log()

    tasks = opt['task'].split(',')
    reports = []
    for task in tasks:
        task_report = _eval_single_world(opt, agent, task)
        reports.append(task_report)

    report = aggregate_named_reports(
        dict(zip(tasks, reports)), micro_average=opt.get('aggregate_micro', False)
    )

    # print announcments and report
    print_announcements(opt)
    logging.info(
        f'Finished evaluating tasks {tasks} using datatype {opt.get("datatype")}'
    )

    print(nice_report(report))
    _save_eval_stats(opt, report)
    return report


@register_script('eval_model', aliases=['em', 'eval'])
class EvalModel(ParlaiScript):
    @classmethod
    def setup_args(cls):
        return setup_args()

    def run(self):
        return eval_model(self.opt)


if __name__ == '__main__':
    EvalModel.main()
