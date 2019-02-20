import time
import logging
from unittest import mock
import pytest
from easydict import EasyDict
import fakeredis

import assemblyline.odm.models.file
import assemblyline.odm.models.submission
from assemblyline.odm.randomizer import random_model_obj
from assemblyline.odm import models


from al_core.dispatching.scheduler import Scheduler
from al_core.dispatching.dispatcher import Dispatcher, DispatchHash, service_queue_name, FileTask, NamedQueue
from al_core.mocking import MockFactory, MockDatastore
from al_core.dispatching.test_scheduler import dummy_service

@pytest.fixture
def clean_redis():
    return fakeredis.FakeStrictRedis()


class Error:
    def __init__(self, data, docid):
        self.id = docid


# class MockDispatchHash:
#     def __init__(self, *args):
#         self._dispatched = {}
#         self._finished = {}
#
#     @staticmethod
#     def _key(file_hash, service):
#         return f"{file_hash}_{service}"
#
#     def all_finished(self):
#         return len(self._dispatched) == 0
#
#     def finished(self, file_hash, service):
#         return self._key(file_hash, service) in self._finished
#
#     def dispatch_time(self, file_hash, service):
#         return self._dispatched.get(self._key(file_hash, service), 0)
#
#     def dispatch(self, file_hash, service):
#         self._dispatched[self._key(file_hash, service)] = time.time()
#
#     def finish(self, file_hash, service, result_key):
#         key = self._key(file_hash, service)
#         self._finished[key] = result_key
#         self._dispatched.pop(key, None)
#
#     def fail_dispatch(self, file_hash, service):
#         self._dispatched[self._key(file_hash, service)] = 0
#
#
# class MockQueue:
#     def __init__(self, *args, **kwargs):
#         self.queue = []
#
#     def push(self, obj):
#         self.queue.append(obj)
#
#     def length(self):
#         return len(self.queue)
#
#     def __len__(self):
#         return len(self.queue)


class Scheduler(Scheduler):
    def __init__(self, *args, **kwargs):
        pass

    def build_schedule(self, *args):
        return [
            ['extract', 'wrench'],
            ['av-a', 'av-b', 'frankenstrings'],
            ['xerox']
        ]

    @property
    def services(self):
        return {
            'extract': dummy_service('extract', 'pre'),
            'wrench': dummy_service('wrench', 'pre'),
            'av-a': dummy_service('av-a', 'core'),
            'av-b': dummy_service('av-b', 'core'),
            'frankenstrings': dummy_service('frankenstrings', 'core'),
            'xerox': dummy_service('xerox', 'post'),
        }


class MockWatcher:
    @staticmethod
    def touch(*args, **kwargs):
        pass


@mock.patch('al_core.dispatching.dispatcher.watcher', MockWatcher)
@mock.patch('al_core.dispatching.dispatcher.Scheduler', Scheduler)
def test_dispatch_file(clean_redis):

    service_queue = lambda name: NamedQueue(service_queue_name(name), clean_redis)

    ds = MockDatastore(collections=['submission', 'result', 'service', 'error', 'file'])
    file_hash = 'totally-a-legit-hash'
    sub = random_model_obj(models.submission.Submission)
    sub.sid = sid = 'first-submission'
    sub.params.ignore_cache = False
    ds.submission.save(sid, sub)

    disp = Dispatcher(ds, clean_redis, clean_redis, logging)
    dh = DispatchHash(sid=sid, client=clean_redis)
    print('==== first dispatch')
    # Submit a problem, and check that it gets added to the dispatch hash
    # and the right service queues
    disp.dispatch_file(FileTask({
        'sid': 'first-submission',
        'file_hash': file_hash,
        'file_type': 'unknown',
        'depth': 0
    }))

    assert dh.dispatch_time(file_hash, 'extract') > 0
    assert dh.dispatch_time(file_hash, 'wrench') > 0
    assert service_queue('extract').length() == 1
    assert len(service_queue('wrench')) == 1

    # Making the same call again should have no effect
    print('==== second dispatch')
    disp.dispatch_file(FileTask({
        'sid': 'first-submission',
        'file_hash': file_hash,
        'file_type': 'unknown',
        'depth': 0
    }))

    assert dh.dispatch_time(file_hash, 'extract') > 0
    assert dh.dispatch_time(file_hash, 'wrench') > 0
    assert len(service_queue('extract')) == 1
    assert len(service_queue('wrench')) == 1
    # assert len(mq) == 4

    # Push back the timestamp in the dispatch hash to simulate a timeout,
    # make sure it gets pushed into that service queue again
    print('==== third dispatch')
    [service_queue(name).delete() for name in disp.scheduler.services]
    dh.fail_recoverable(file_hash, 'extract')

    disp.dispatch_file(FileTask({
        'sid': 'first-submission',
        'file_hash': file_hash,
        'file_type': 'unknown',
        'depth': 0
    }))

    assert dh.dispatch_time(file_hash, 'extract') > 0
    assert dh.dispatch_time(file_hash, 'wrench') > 0
    assert service_queue('extract').length() == 1
    # assert len(mq) == 1

    # Mark extract as finished in the dispatch table, add a result object
    # for the wrench service, it should move to the second batch of services
    print('==== fourth dispatch')
    [service_queue(name).delete() for name in disp.scheduler.services]
    dh.finish(file_hash, 'extract', 'result-key')
    wrench_result_key = disp.build_result_key(
        file_hash=file_hash,
        service=disp.scheduler.services.get('wrench'),
        submission=sub
    )
    print('wrench result key', wrench_result_key)
    ds.result.save(wrench_result_key, EasyDict(drop_file=False))

    disp.dispatch_file(FileTask({
        'sid': 'first-submission',
        'file_hash': file_hash,
        'file_type': 'unknown',
        'depth': 0
    }))

    assert dh.finished(file_hash, 'extract')
    assert dh.finished(file_hash, 'wrench')
    assert len(service_queue('av-a')) == 1
    assert len(service_queue('av-b')) == 1
    assert len(service_queue('frankenstrings')) == 1

    # Have the AVs fail, frankenstrings finishes
    print('==== fifth dispatch')
    [service_queue(name).delete() for name in disp.scheduler.services]
    dh.fail_nonrecoverable(file_hash, 'av-a', 'error-a')
    dh.fail_nonrecoverable(file_hash, 'av-b', 'error-b')
    dh.finish(file_hash, 'frankenstrings', 'result-key')

    disp.dispatch_file(FileTask({
        'sid': 'first-submission',
        'file_hash': file_hash,
        'file_type': 'unknown',
        'depth': 0
    }))

    assert dh.finished(file_hash, 'av-a')
    assert dh.finished(file_hash, 'av-b')
    assert dh.finished(file_hash, 'frankenstrings')
    assert len(service_queue('xerox')) == 1

    # Finish the xerox service and check if the submission completion got checked
    print('==== sixth dispatch')
    [service_queue(name).delete() for name in disp.scheduler.services]
    dh.finish(file_hash, 'xerox', 'result-key')

    disp.dispatch_file(FileTask({
        'sid': 'first-submission',
        'file_hash': file_hash,
        'file_type': 'unknown',
        'depth': 0
    }))

    assert dh.finished(file_hash, 'xerox')
    assert len(disp.submission_queue) == 1


@mock.patch('al_core.dispatching.dispatcher.watcher', MockWatcher)
@mock.patch('al_core.dispatching.dispatcher.Scheduler', Scheduler)
def test_dispatch_submission(clean_redis):
    ds = MockDatastore(collections=['submission', 'result', 'service', 'error', 'file'])
    file_hash = 'totally-a-legit-hash'

    ds.file.save(file_hash, random_model_obj(models.file.File))
    ds.file.get(file_hash).sha256 = file_hash
    # ds.file.get(file_hash).sha256 = ''

    submission = random_model_obj(models.submission.Submission)
    submission.files.clear()
    submission.files.append(dict(
        name='./file',
        sha256=file_hash
    ))

    submission.sid = 'first-submission'
    ds.submission.save(submission.sid, submission)

    disp = Dispatcher(ds, logger=logging, redis=clean_redis, redis_persist=clean_redis)
    print('==== first dispatch')
    # Submit a problem, and check that it gets added to the dispatch hash
    # and the right service queues
    disp.dispatch_submission(submission)
