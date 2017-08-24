# -*- coding: utf-8 -*-

from abc import ABCMeta, abstractmethod
import apricotpy
import copy
from enum import Enum
import logging
import time
from collections import namedtuple

import plum.stack as _stack
from plum.process_listener import ProcessListener
from plum.process_monitor import MONITOR
from plum.process_spec import ProcessSpec
from plum.process_states import *
from plum.utils import protected
from plum.wait import WaitOn
from . import utils

__all__ = ['Process']

_LOGGER = logging.getLogger(__name__)

Wait = namedtuple('Wait', ['on', 'callback'])


class Process(apricotpy.persistable.AwaitableLoopObject):
    """
    The Process class is the base for any unit of work in plum.

    Once a process is created it may be started by calling play() at which
    point it is said to be 'playing', like a tape.  It can then be paused by
    calling pause() which will only be acted on at the next state transition
    OR if the process is in the WAITING state in which case it will pause
    immediately.  It can be resumed with a call to play().

    A process can be in one of the following states:

    * CREATED
    * RUNNING
    * WAITING
    * STOPPED
    * FAILED

    as defined in the :class:`ProcessState` enum.

    The possible transitions between states are::

                              _(reenter)_
                              |         |
        CREATED---on_start,on_run-->RUNNING---on_finish,on_stop-->STOPPED
                                    |     ^               |         ^
                               on_wait on_resume,on_run   |   on_abort,on_stop
                                    v     |               |         |
                                    WAITING----------------     [any state]

        [any state]---on_fail-->FAILED

    ::

    When a Process enters a state is always gets a corresponding message, e.g.
    on entering RUNNING it will receive the on_run message.  These are
    always called immediately after that state is entered but before being
    executed.
    """
    __metaclass__ = ABCMeta

    # Static class stuff ######################
    _spec_type = ProcessSpec

    class BundleKeys(Enum):
        """
        String keys used by the process to save its state in the state bundle.

        See :func:`create_from`, :func:`save_instance_state` and :func:`load_instance_state`.
        """
        CREATION_TIME = 'creation_time'
        CLASS_NAME = 'class_name'
        INPUTS = 'inputs'
        OUTPUTS = 'outputs'
        PID = 'pid'
        STATE = 'state'
        FINISHED = 'finished'
        TERMINATED = 'terminated'
        WAIT_ON = 'wait_on'

    @staticmethod
    def _is_wait_retval(retval):
        """
        Determine if the value provided is a valid Wait retval which consists
        of a 2-tuple of a WaitOn and a callback function (or None) to be called
        after the wait on is ready

        :param retval: The return value from a step to check
        :return: True if it is a valid wait object, False otherwise
        """
        return (isinstance(retval, tuple) and
                len(retval) == 2 and
                isinstance(retval[0], WaitOn))

    @classmethod
    def spec(cls):
        try:
            return cls.__getattribute__(cls, '_spec')
        except AttributeError:
            cls._spec = cls._spec_type()
            cls.__called = False
            cls.define(cls._spec)
            assert cls.__called, \
                "Process.define() was not called by {}\n" \
                "Hint: Did you forget to call the superclass method in your define? " \
                "Try: super({}, cls).define(spec)".format(cls, cls.__name__)
            return cls._spec

    @classmethod
    def get_name(cls):
        return cls.__name__

    @classmethod
    def define(cls, spec):
        cls.__called = True

    @classmethod
    def get_description(cls):
        """
        Get a human readable description of what this :class:`Process` does.

        :return: The description.
        :rtype: str
        """
        desc = []
        if cls.__doc__:
            desc.append("Description")
            desc.append("===========")
            desc.append(cls.__doc__)

        spec_desc = cls.spec().get_description()
        if spec_desc:
            desc.append("Specification")
            desc.append("=============")
            desc.append(spec_desc)

        return "\n".join(desc)

    def __init__(self, inputs=None, pid=None, logger=None):
        """
        The signature of the constructor should not be changed by subclassing
        processes.

        :param inputs: A dictionary of the process inputs
        :type inputs: dict
        :param pid: The process ID, if not a unique pid will be chosen
        :param logger: An optional logger for the process to use
        :type logger: :class:`logging.Logger`
        """
        super(Process, self).__init__()

        # Don't allow the spec to be changed anymore
        self.spec().seal()

        # Setup runtime state
        self.__init(logger)

        # Input/output
        self._check_inputs(inputs)
        self._raw_inputs = None if inputs is None else utils.AttributesFrozendict(inputs)
        self._parsed_inputs = utils.AttributesFrozendict(self.create_input_args(self.raw_inputs))
        self._outputs = {}

        # Set up a process ID
        if pid is None:
            self._pid = self.uuid
        else:
            self._pid = pid

        # State stuff
        self._CREATION_TIME = time.time()
        self._finished = False
        self._terminated = False
        self.__state_bundle = None

    @property
    def creation_time(self):
        """
        The creation time of this Process as returned by time.time() when instantiated
        :return: The creation time
        :rtype: float
        """
        return self._CREATION_TIME

    @property
    def pid(self):
        return self._pid

    @property
    def raw_inputs(self):
        return self._raw_inputs

    @property
    def inputs(self):
        return self._parsed_inputs

    @property
    def outputs(self):
        """
        Get the current outputs emitted by the Process.  These may grow over
        time as the process runs.

        :return: A mapping of {output_port: value} outputs
        :rtype: dict
        """
        return self._outputs

    @property
    def state(self):
        return self._state.label

    @property
    def logger(self):
        """
        Get the logger for this class.  Can be None.

        :return: The logger.
        :rtype: :class:`logging.Logger`
        """
        if self.__logger is not None:
            return self.__logger
        else:
            return _LOGGER

    def has_finished(self):
        """
        Has the process finished i.e. completed running normally, without abort
        or an exception.

        :return: True if finished, False otherwise
        :rtype: bool
        """
        return self._finished

    def has_failed(self):
        """
        Has the process failed i.e. an exception was raised

        :return: True if an unhandled exception was raised, False otherwise
        :rtype: bool
        """
        return self.get_exc_info() is not None

    def has_terminated(self):
        """
        Has the process terminated

        :return: True if the process is STOPPED or FAILED, False otherwise
        :rtype: bool
        """
        return self._terminated

    def has_aborted(self):
        try:
            return self._state.get_aborted()
        except AttributeError:
            return False

    def get_abort_msg(self):
        try:
            return self._state.get_abort_msg()
        except AttributeError:
            return None

    def get_waiting_on(self):
        """
        Get the awaitable this process is waiting on, or None.

        :return: The awaitable or None
        :rtype: :class:`apricotpy.Awaitable` or None
        """
        try:
            return self._state.awaiting()
        except AttributeError:
            return None

    def get_exception(self):
        exc_info = self.get_exc_info()
        if exc_info is None:
            return None

        return exc_info[1]

    def get_exc_info(self):
        """
        If this process produced an exception that caused it to fail during its
        execution then it will have store the execution information as obtained
        from sys.exc_info(), this method returns it.  If there was no exception
        then None is returned.

        :return: The exception info if process failed, None otherwise
        """
        try:
            return self._state.get_exc_info()
        except AttributeError:
            return None

    def save_instance_state(self, out_state):
        """
        Ask the process to save its current instance state.

        :param out_state: A bundle to save the state to
        :type out_state: :class:`apricotpy.Bundle`
        """
        super(Process, self).save_instance_state(out_state)
        # Immutables first
        out_state[self.BundleKeys.CREATION_TIME.value] = self.creation_time
        out_state[self.BundleKeys.CLASS_NAME.value] = utils.fullname(self)
        out_state[self.BundleKeys.PID.value] = self.pid

        # Now state stuff
        state_bundle = {}
        if self._state is None:
            out_state[self.BundleKeys.STATE.value] = None
        else:
            self._state.save_instance_state(state_bundle)
            out_state[self.BundleKeys.STATE.value] = state_bundle

        out_state[self.BundleKeys.FINISHED.value] = self._finished
        out_state[self.BundleKeys.TERMINATED.value] = self._terminated

        # Inputs/outputs
        out_state[self.BundleKeys.INPUTS.value] = self.raw_inputs
        out_state[self.BundleKeys.OUTPUTS.value] = self._outputs

    @protected
    def load_instance_state(self, saved_state, loop):
        super(Process, self).load_instance_state(saved_state, loop)
        self.__init(None)

        # Immutable stuff
        self._CREATION_TIME = saved_state[self.BundleKeys.CREATION_TIME.value]
        self._pid = saved_state[self.BundleKeys.PID.value]

        # State stuff
        self._finished = saved_state[self.BundleKeys.FINISHED.value]
        self._terminated = saved_state[self.BundleKeys.TERMINATED.value]
        self.__saved_state = saved_state[self.BundleKeys.STATE.value]

        # Inputs/outputs
        if saved_state[self.BundleKeys.INPUTS.value] is not None:
            self._raw_inputs = utils.AttributesFrozendict(saved_state[self.BundleKeys.INPUTS.value])
        else:
            self._raw_inputs = None
        self._parsed_inputs = utils.AttributesFrozendict(self.create_input_args(self.raw_inputs))
        self._outputs = copy.deepcopy(saved_state[self.BundleKeys.OUTPUTS.value])

    def on_loop_inserted(self, loop):
        super(Process, self).on_loop_inserted(loop)

        if self.__saved_state is None:
            self._set_state(Created(self))
        else:
            self._set_state(load_state(self, self.__saved_state))

    def _execute_state(self, fut=None):
        try:
            _stack.push(self)
            self._state.execute()
        finally:
            _stack.pop(self)

            if fut is not None:
                fut.set_result(self._state.label)

    def abort(self, msg=None):
        """
        Abort a playing process.  Can optionally provide a message with
        the abort.  This can be called from another thread.

        :param msg: The abort message
        :type msg: str
        """
        self.log_with_pid(logging.INFO, "aborting")

        self._loop_check()

        fut = self.loop().create_future()
        self.loop().call_soon(self._do_abort, fut, msg)
        return fut

    def _do_abort(self, fut, msg=None):
        if self.has_terminated():
            fut.set_result(False)
            return

        # Abort the current state
        self._state.abort()
        self._set_state(Stopped(self, abort=True, abort_msg=msg))
        self._state.execute()

        fut.set_result(True)

    def play(self):
        if self.is_playing():
            return

        self._state.enter(self._state.label)
        self.__paused = False

    def pause(self):
        if not self.is_playing():
            return

        self._state.abort()
        self.__paused = True

    def is_playing(self):
        return not self.__paused

    def add_process_listener(self, listener):
        assert (listener != self), "Cannot listen to yourself!"
        self.__event_helper.add_listener(listener)

    def remove_process_listener(self, listener):
        self.__event_helper.remove_listener(listener)

    def listen_scope(self, listener):
        return ListenContext(self, listener)

    @protected
    def set_logger(self, logger):
        self.__logger = logger

    @protected
    def log_with_pid(self, level, msg):
        self.logger.log(level, "{}: {}".format(self.pid, msg))

    # region Process messages
    # Make sure to call the superclass method if your override any of these
    @protected
    def on_create(self):
        """
        Called when the process is created.
        """
        # In this case there is no message fired because no one could have
        # registered themselves as a listener by this point in the lifecycle.

        self.__called = True

    @protected
    def on_start(self):
        """
        Called when this process is about to run for the first time.


        Any class overriding this method should make sure to call the super
        method, usually at the end of the function.
        """
        self._fire_event(ProcessListener.on_process_start)
        self._send_message('start')

        self.__called = True

    @protected
    def on_run(self):
        """
        Called when the process is about to run some code either for the first
        time (in which case an on_start message would have been received) or
        after something it was waiting on has finished (in which case an
        on_continue message would have been received).

        Any class overriding this method should make sure to call the super
        method.
        """
        self._fire_event(ProcessListener.on_process_run)
        self._send_message('run')

        self.__called = True

    @protected
    def on_wait(self, awaiting_uuid):
        """
        Called when the process is about to enter the WAITING state
        """
        self._fire_event(ProcessListener.on_process_wait)
        self._send_message('wait', {'awaiting': awaiting_uuid})

        self.__called = True

    @protected
    def on_resume(self):
        self._fire_event(ProcessListener.on_process_resume)
        self._send_message('resume')

        self.__called = True

    @protected
    def on_abort(self, abort_msg):
        """
        Called when the process has been asked to abort itself.
        """
        self._fire_event(ProcessListener.on_process_abort)
        self._send_message('abort', {'msg': abort_msg})

        self.__called = True

    @protected
    def on_finish(self):
        """
        Called when the process has finished and the outputs have passed
        checks
        """
        self._check_outputs()
        self._finished = True
        self._fire_event(ProcessListener.on_process_finish)
        self._send_message('finish')

        self.__called = True

    @protected
    def on_stop(self):
        self._fire_event(ProcessListener.on_process_stop)
        self._send_message('stop')

        self.__called = True

    @protected
    def on_fail(self):
        """
        Called if the process raised an exception.
        """
        self._fire_event(ProcessListener.on_process_fail)
        self._send_message('fail')

        self.__called = True

    @protected
    def on_terminate(self):
        """
        Called when the process reaches a terminal state.
        """
        self._fire_event(ProcessListener.on_process_terminate)
        self._send_message('terminate')

        self.__called = True

    def on_output_emitted(self, output_port, value, dynamic):
        self.__event_helper.fire_event(ProcessListener.on_output_emitted,
                                       self, output_port, value, dynamic)

    # endregion

    @protected
    def do_run(self):
        return self._run(**(self.inputs if self.inputs is not None else {}))

    @protected
    def out(self, output_port, value):
        dynamic = False
        # Do checks on the outputs
        try:
            # Check types (if known)
            port = self.spec().get_output(output_port)
        except KeyError:
            if self.spec().has_dynamic_output():
                dynamic = True
                port = self.spec().get_dynamic_output()
            else:
                raise TypeError(
                    "Process trying to output on unknown output port {}, "
                    "and does not have a dynamic output port in spec.".
                        format(output_port))

            if port.valid_type is not None and not isinstance(value, port.valid_type):
                raise TypeError(
                    "Process returned output '{}' of wrong type."
                    "Expected '{}', got '{}'".
                        format(output_port, port.valid_type, type(value)))

        self._outputs[output_port] = value
        self.on_output_emitted(output_port, value, dynamic)

    @protected
    def create_input_args(self, inputs):
        """
        Take the passed input arguments and fill in any default values for
        inputs that have no been supplied.

        Preconditions:
        * All required inputs have been supplied

        :param inputs: The supplied input values.
        :return: A dictionary of inputs including any with default values
        """
        if inputs is None:
            ins = {}
        else:
            ins = dict(inputs)
        # Go through the spec filling in any default and checking for required
        # inputs
        for name, port in self.spec().inputs.iteritems():
            if name not in ins:
                if port.default:
                    ins[name] = port.default
                elif port.required:
                    raise ValueError(
                        "Value not supplied for required inputs port {}".format(name)
                    )

        return ins

    def __init(self, logger):
        """
        Common place to put all runtime state variables i.e. those that don't need
        to be persisted.  This can be called from the constructor or
        load_instance_state.
        """
        self._state = None
        self.__logger = logger
        self.__saved_state = None
        self.__paused = False

        # Events and running
        self.__event_helper = utils.EventHelper(ProcessListener)

        # Flag to make sure all the necessary event methods were called
        self.__called = False

    # region State event/transition methods

    def _on_start_playing(self):
        _stack.push(self)
        MONITOR.register_process(self)

    def _on_stop_playing(self):
        """
        WARNING: No state changes should be made after this call.
        """
        MONITOR.deregister_process(self)
        _stack.pop(self)

        if self.has_terminated():
            # There will be no more messages so remove the listeners.  Otherwise we
            # may continue to hold references to them and stop them being garbage
            # collected
            self.__event_helper.remove_all_listeners()

    def _on_create(self):
        self._call_with_super_check(self.on_create)

    def _on_start(self):
        self._call_with_super_check(self.on_start)

    def _on_resume(self):
        self._call_with_super_check(self.on_resume)

    def _on_run(self):
        self._call_with_super_check(self.on_run)

    def _on_wait(self, awaiting_uuid):
        self._call_with_super_check(self.on_wait, awaiting_uuid)

    def _on_finish(self):
        self._call_with_super_check(self.on_finish)

    def _on_abort(self, msg):
        self._call_with_super_check(self.on_abort, msg)

    def _on_stop(self, msg):
        self._call_with_super_check(self.on_stop)

    def _on_fail(self, exc_info):
        self._call_with_super_check(self.on_fail)

    def _fire_event(self, event):
        self.loop().call_soon(self.__event_helper.fire_event, event, self)

    def _send_message(self, subject, body_=None):
        body = {'uuid': self.uuid}
        if body_ is not None:
            body.update(body_)
        self.send_message('process.{}.{}'.format(self.pid, subject), body)

    def _terminate(self):
        self._terminated = True
        self._call_with_super_check(self.on_terminate)

    def _call_with_super_check(self, fn, *args, **kwargs):
        self.__called = False
        fn(*args, **kwargs)
        assert self.__called, \
            "{} was not called\n" \
            "Hint: Did you forget to call the superclass method?".format(fn.__name__)

    # endregion

    def _check_inputs(self, inputs):
        # Check the inputs meet the requirements
        valid, msg = self.spec().validate(inputs)
        if not valid:
            raise ValueError(msg)

    def _check_outputs(self):
        # Check that the necessary outputs have been emitted
        for name, port in self.spec().outputs.iteritems():
            valid, msg = port.validate(self._outputs.get(name, None))
            if not valid:
                raise RuntimeError("Process {} failed because {}".
                                   format(self.get_name(), msg))

    def _set_state(self, state):
        if self._state is None:
            previous_state = None
        else:
            previous_state = self._state.label
            self._state.exit()

        self._state = state
        self._state.enter(previous_state)

    def _loop_check(self):
        assert self.in_loop(), "The process is not in the event loop"

    @abstractmethod
    def _run(self, **kwargs):
        pass


class ListenContext(object):
    """
    A context manager for listening to the Process.
    
    A typical usage would be:
    with ListenContext(producer, listener):
        # Producer generates messages that the listener gets
        pass
    """

    def __init__(self, producer, *args, **kwargs):
        self._producer = producer
        self._args = args
        self._kwargs = kwargs

    def __enter__(self):
        self._producer.add_process_listener(*self._args, **self._kwargs)
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self._producer.remove_process_listener(*self._args, **self._kwargs)
