# -*- coding: utf-8 -*-
'''
Execute batch runs
'''

# Import python libs
from __future__ import absolute_import, print_function, unicode_literals
import tornado

# Import salt libs
import salt.client

# pylint: enable=import-error,no-name-in-module,redefined-builtin
import logging
import fnmatch

log = logging.getLogger(__name__)

from salt.cli.batch import _get_bnum, _batch_get_opts, _batch_get_eauth


class BatchAsync(object):
    '''
    Manage the execution of batch runs
    '''
    def __init__(self, parent_opts, jid_gen, clear_load):
        ioloop = tornado.ioloop.IOLoop.current()
        self.local = salt.client.get_local_client(parent_opts['conf_file'])
        clear_load['gather_job_timeout'] = clear_load['kwargs'].pop('gather_job_timeout')
        self.opts = _batch_get_opts(
            clear_load.pop('tgt'),
            clear_load.pop('fun'),
            clear_load['kwargs'].pop('batch'),
            self.local.opts,
            **clear_load)
        self.eauth = _batch_get_eauth(clear_load['kwargs'])
        self.minions = set()
        self.down_minions = set()
        self.timedout_minions = set()
        self.done_minions = set()
        self.to_run = set()
        self.active = set()
        self.initialized = False
        self.ping_jid = jid_gen()
        self.batch_jid = jid_gen()
        self.find_job_jid = jid_gen()
        self.find_job_returned = set()
        self.event = salt.utils.event.get_event(
            'master',
            self.opts['sock_dir'],
            self.opts['transport'],
            opts=self.opts,
            listen=True,
            io_loop=ioloop,
            keep_loop=True)
        self.__set_event_handler()

    def __set_event_handler(self):
        ping_return_pattern = 'salt/job/{0}/ret/*'.format(self.ping_jid)
        batch_return_pattern = 'salt/job/{0}/ret/*'.format(self.batch_jid)
        find_job_return_pattern = 'salt/job/{0}/ret/*'.format(self.find_job_jid)
        self.event.subscribe(ping_return_pattern, match_type='glob')
        self.event.subscribe(batch_return_pattern, match_type='glob')
        self.event.patterns = {
            (ping_return_pattern, 'ping_return'),
            (batch_return_pattern, 'batch_run'),
            (find_job_return_pattern, 'find_job_return')
        }
        if not self.event.subscriber.connected():
            self.event.set_event_handler(self.__event_handler)

    def __event_handler(self, raw):
        mtag, data = self.event.unpack(raw, self.event.serial)
        for (pattern, op) in self.event.patterns:
            if fnmatch.fnmatch(mtag, pattern):
                minion = data['id']
                if op == 'ping_return':
                    self.minions.add(minion)
                    self.down_minions.remove(minion)
                    self.batch_size = _get_bnum(self.opts, self.minions, True)
                    self.to_run = self.minions.difference(self.done_minions).difference(self.active)
                elif op == 'find_job_return':
                    self.find_job_returned.add(minion)
                elif op == 'batch_run':
                    if minion in self.active:
                        self.active.remove(minion)
                        self.done_minions.add(minion)
                    from salt.master import mylogger
                    if self.done_minions == self.minions.difference(self.timedout_minions):
                        # TODO
                        # if not all available minions finish the batch
                        # the event handler connection is not closed
                        mylogger.info('Closing: %s %s %s', self.done_minions, self.timedout_minions, self.down_minions)
                        self.event.close_pub()
                    else:
                        # call later so that we maybe gather more returns
                        self.event.io_loop.call_later(1, self.next)
                if not self.initialized:
                    #start batching even if not all minions respond to ping
                    self.event.io_loop.call_later(
                        self.opts['gather_job_timeout'], self.next)
                    self.initialized = True

    def _get_next(self):
        next_batch_size = min(
            len(self.to_run),                   # partial batch (all left)
            self.batch_size - len(self.active)  # full batch or available slots
        )
        next_batch = set()
        for i in range(next_batch_size):
            next_batch.add(self.to_run.pop())
        return next_batch

    @tornado.gen.coroutine
    def check_find_job(self, minions):
        from salt.master import mylogger
        did_not_return = minions.difference(self.find_job_returned)
        if did_not_return:
            mylogger.info('Not returned: %s', did_not_return)
            for minion in did_not_return:
                if minion in self.find_job_returned:
                    self.find_job_returned.remove(minion)
                if minion in self.active:
                    self.active.remove(minion)
                self.timedout_minions.add(minion)
        running = minions.difference(did_not_return).difference(self.done_minions).difference(self.timedout_minions)
        if running:
            self.event.io_loop.add_callback(self.find_job, running)

    @tornado.gen.coroutine
    def find_job(self, minions):
        from salt.master import mylogger
        not_done = minions.difference(self.done_minions)
        ping_return = yield self.local.run_job_async(
            not_done,
            'saltutil.find_job',
            [self.batch_jid],
            'list',
            gather_job_timeout=self.opts['gather_job_timeout'],
            jid=self.find_job_jid,
            **self.eauth)
        self.event.io_loop.call_later(
            self.opts['gather_job_timeout'],
            self.check_find_job,
            not_done)

    @tornado.gen.coroutine
    def start(self):
        ping_return = yield self.local.run_job_async(
            self.opts['tgt'],
            'test.ping',
            [],
            self.opts.get(
                'selected_target_option',
                self.opts.get('tgt_type', 'glob')
            ),
            gather_job_timeout=self.opts['gather_job_timeout'],
            jid=self.ping_jid,
            **self.eauth)
        self.down_minions = set(ping_return['minions'])

    @tornado.gen.coroutine
    def next(self):
        next_batch = self._get_next()
        if next_batch:
            yield self.local.run_job_async(
                next_batch,
                self.opts['fun'],
                self.opts['arg'],
                'list',
                raw=self.opts.get('raw', False),
                ret=self.opts.get('return', ''),
                gather_job_timeout=self.opts['gather_job_timeout'],
                jid=self.batch_jid,
                **self.eauth)
            # TODO add parameter for find_job - should use gather_job_timeout?
            self.event.io_loop.call_later(10, self.find_job, set(next_batch))
            self.active = self.active.union(next_batch)