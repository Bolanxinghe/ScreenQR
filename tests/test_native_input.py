"""Injected fake APIs only: never SendInput, GUI, native query or external app.

ABI/layout and normalization checks do not establish real cursor/selection or
business behavior; real independent extent and result checks remain required.
"""
import ctypes
from dataclasses import FrozenInstanceError
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_controller import fixture, Clock
from screenqr.handoff import NativeSnapshot
from screenqr.native_input import (WeChatInput, InputBlocked, InputOutcomeUnknown,
    _Api, _INPUT, _MOUSEINPUT, _KEYBDINPUT, _absolute_point)


class FakeApi:
    def __init__(self):
        self.foreground, self.idle = True, True
        self.calls = []
        self.send_hook = None
        self.idle_hook = None

    def own_foreground(self):
        return self.foreground

    def idle_fixed_desktop(self):
        if self.idle_hook:
            self.idle_hook()
        return self.idle

    def send(self, events):
        self.calls.append(events)
        return self.send_hook(events) if self.send_hook else len(events)


class FakeDesktop:
    def __init__(self):
        self.awareness = 2
        self.held = set()
        self.metrics = {80:1,76:0,77:0,78:3200,79:2000}

    def GetThreadDpiAwarenessContext(self):
        return 123

    def GetAwarenessFromDpiAwarenessContext(self, context):
        return self.awareness

    def GetSystemMetrics(self, metric):
        return self.metrics[metric]

    def GetAsyncKeyState(self, vk):
        return 0x8000 if vk in self.held else 1  # Low historical bit must be ignored.


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.api = Clock(), FakeApi()
        self.dispatcher = WeChatInput(clock=self.clock,_api=self.api)
        self.request, self.trigger = str(uuid4()), str(uuid4())
        _, _, probe = fixture()
        self.instance = NativeSnapshot.from_probe(probe,started_ns=1,finished_ns=2,
            fixed_file_version=(4,1,15,13)).instance
        self.deadline = 20_000

    def trigger_once(self, validate=lambda: True):
        self.dispatcher.trigger_once(self.request,self.trigger,self.deadline,validate)

    def select_once(self, instance=None, validate=lambda: True):
        self.dispatcher.select_once(self.instance if instance is None else instance,(100,100,500,600),
            self.request,self.deadline,validate)

    def test_input_abi_and_fake_send_uses_actual_struct_size(self):
        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(_INPUT),40 if pointer==8 else 28)
        self.assertEqual(_INPUT.data.offset,8 if pointer==8 else 4)
        self.assertEqual(ctypes.sizeof(_MOUSEINPUT),32 if pointer==8 else 24)
        self.assertEqual(ctypes.sizeof(_KEYBDINPUT),24 if pointer==8 else 16)
        # Exercise the _Api packing routine with a fake user DLL, not _Api().
        packed = []
        class User:
            def SendInput(self, count, array, size):
                packed.append((count,size,[array[i].ki.wVk for i in range(count)]))
                return count
        api = object.__new__(_Api)
        api.user = User()
        self.trigger_once()
        self.assertEqual(api.send(self.api.calls[0]),4)
        self.assertEqual(packed,[(4,ctypes.sizeof(_INPUT),[18,65,65,18])])

    def test_fixed_physical_pixel_centers_and_drag_flags(self):
        for limit, axis in ((3200,0),(2000,1)):
            for value in range(limit):
                point = _absolute_point(value,0) if axis==0 else _absolute_point(0,value)
                self.assertEqual(point[axis]*limit//65536,value)
        for point in ((-1,0),(3200,0),(0,2000),(True,0)):
            with self.assertRaises(ValueError):
                _absolute_point(*point)
        self.select_once()
        events = self.api.calls[0]
        self.assertEqual([(event.mi.dx,event.mi.dy) for event in events],
            [_absolute_point(100,100),_absolute_point(100,100),_absolute_point(500,600),_absolute_point(500,600)])
        self.assertEqual([event.mi.dwFlags for event in events],[0xE001,0xE003,0xE001,0xE005])

    def test_single_drag_batch_retains_noncoalesced_held_move_and_mouse_input_abi(self):
        self.select_once()
        self.assertEqual(len(self.api.calls),1)
        observed=[]
        class User:
            def SendInput(self,count,array,size):
                observed.append((count,size,[array[i].mi.dwFlags for i in range(count)]))
                return count
        api=object.__new__(_Api)
        api.user=User()
        self.assertEqual(api.send(self.api.calls[0]),4)
        count,size,flags=observed[0]
        self.assertEqual((count,size),(4,40 if ctypes.sizeof(ctypes.c_void_p)==8 else 28))
        self.assertTrue(all(flag & 0x2000 for flag in flags))
        self.assertEqual(flags[1] & 6,2)  # Down precedes the independent held move.
        self.assertEqual(flags[2] & 6,0)  # Held move does not press/release again.
        self.assertEqual(flags[3] & 6,4)

    def test_actual_idle_check_rejects_unaware_thread_or_held_A_Escape(self):
        api = object.__new__(_Api)
        api.user = FakeDesktop()
        self.assertTrue(api.idle_fixed_desktop())
        api.user.awareness = 0
        self.assertFalse(api.idle_fixed_desktop())
        self.assertEqual(api.last_idle_error_code,'dpi_awareness_mismatch')
        api.user.awareness = 2
        for key in (18,65,27,1):
            api.user.held = {key}
            self.assertFalse(api.idle_fixed_desktop())
            self.assertEqual(api.last_idle_error_code,'user_keys_active')
        api.user.held = set()
        api.user.metrics[78] = 1600
        self.assertFalse(api.idle_fixed_desktop())
        self.assertEqual(api.last_idle_error_code,'desktop_profile_mismatch')

    def test_foreground_or_cancelled_final_validation_blocks_before_send(self):
        self.api.foreground = False
        with self.assertRaises(InputBlocked):
            self.trigger_once()
        self.assertFalse(self.api.calls)
        self.assertEqual(self.dispatcher.last_report.error_code,'trigger_own_foreground_changed')
        self.assertEqual(self.dispatcher.last_report.state,'NOT_DISPATCHED')
        self.api.foreground = True
        current = {'value':True}
        self.api.idle_hook = lambda: current.update(value=False)
        with self.assertRaises(InputBlocked):
            self.trigger_once(validate=lambda: current['value'])
        self.assertFalse(self.api.calls)
        self.assertEqual(self.dispatcher.last_report.error_code,'source_or_native_validation_failed')
        self.assertEqual(self.dispatcher.last_report.send_input_calls,0)

    def test_expired_or_invalid_identity_does_not_send(self):
        with self.assertRaises(InputBlocked):
            self.dispatcher.trigger_once('not-a-UUID',self.trigger,self.deadline,lambda:True)
        with self.assertRaises(InputBlocked):
            self.dispatcher.trigger_once(self.request,'not-a-UUID',self.deadline,lambda:True)
        self.deadline = 0
        with self.assertRaises(InputBlocked):
            self.trigger_once()
        self.assertFalse(self.api.calls)

    def test_partial_raised_late_or_bad_clock_dispatch_latches_unknown_and_never_recovers_by_input(self):
        for failure in ('partial','raised','late','regressed'):
            with self.subTest(failure=failure):
                self.setUp()
                def send(events):
                    if failure=='raised':
                        raise RuntimeError('synthetic send exception')
                    if failure=='late':
                        self.clock.value = self.deadline
                    if failure=='regressed':
                        self.clock.value = 0
                    return len(events)-1 if failure=='partial' else len(events)
                self.api.send_hook = send
                with self.assertRaises(InputOutcomeUnknown):
                    self.trigger_once()
                report = self.dispatcher.last_report
                self.assertEqual(report.state,'OUTCOME_UNKNOWN')
                self.assertEqual(report.send_input_calls,1)
                self.assertEqual(report.operation_send_input_calls,1)
                self.assertEqual(report.returned_event_count,
                    None if failure=='raised' else 3 if failure=='partial' else 4)
                self.assertEqual(report.consumed_operations,('trigger',))
                self.assertTrue(report.outcome_unknown)
                with self.assertRaises(InputBlocked):
                    self.trigger_once()
                self.assertEqual(self.dispatcher.last_report.state,'NOT_DISPATCHED')
                self.assertEqual(self.dispatcher.last_report.operation_send_input_calls,0)
                self.assertTrue(self.dispatcher.last_report.outcome_unknown)
                with self.assertRaises(InputBlocked):
                    self.dispatcher.cancel_once(self.instance,self.request,self.deadline,lambda:True)
                self.assertEqual(len(self.api.calls),1)

    def test_early_uuid_clock_profile_event_build_and_validation_errors_record_zero_dispatch(self):
        cases = (
            ('trigger_uuid', 'invalid_trigger_identity'),
            ('clock', 'dispatch_clock_invalid_or_regressed'),
            ('profile', 'desktop_profile_mismatch'),
            ('event_build', 'dispatch_precheck_raised'),
            ('validator_raised', 'source_or_native_validation_raised'),
        )
        for case, expected in cases:
            with self.subTest(case=case):
                self.setUp()
                def run():
                    if case=='trigger_uuid':
                        self.dispatcher.trigger_once(self.request,'invalid',self.deadline,lambda:True)
                    elif case=='event_build':
                        with patch('screenqr.native_input._key',side_effect=ValueError('private synthetic text')):
                            self.trigger_once()
                    elif case=='validator_raised':
                        def fail(): raise RuntimeError('private synthetic text')
                        self.trigger_once(validate=fail)
                    else:
                        if case=='clock': self.clock.value=-10
                        if case=='profile':
                            self.api.idle=False
                            self.api.last_idle_error_code='desktop_profile_mismatch'
                        self.trigger_once()
                with self.assertRaises((InputBlocked,ValueError)):
                    run()
                report=self.dispatcher.last_report
                self.assertEqual((report.state,report.error_code),('NOT_DISPATCHED',expected))
                self.assertEqual((report.send_input_calls,report.operation_send_input_calls),(0,0))
                self.assertEqual(report.consumed_operations,())
                self.assertFalse(report.outcome_unknown)
                self.assertFalse(self.api.calls)
                self.assertNotIn('private synthetic text',repr(self.dispatcher.metadata_only()))

    def test_success_report_is_immutable_detached_and_only_confirms_dispatch(self):
        self.trigger_once()
        report=self.dispatcher.last_report
        self.assertEqual((report.operation,report.state,report.error_code),
            ('trigger','DISPATCH_RETURNED','dispatch_returned'))
        self.assertEqual((report.send_input_calls,report.returned_event_count),(1,4))
        self.assertFalse(report.outcome_unknown)
        with self.assertRaises(FrozenInstanceError):
            report.state='business_success'
        data=self.dispatcher.metadata_only()
        self.assertTrue(data['metadata_only'])
        self.assertFalse(data['application_acceptance_verified'])
        self.assertNotIn(self.request,repr(data))
        data['state']='business_success'
        self.assertEqual(self.dispatcher.last_report.state,'DISPATCH_RETURNED')

    def test_zero_return_caches_immediate_win32_error_without_claiming_cause_or_retry(self):
        order=[]
        class User:
            def SendInput(self, count, array, size):
                order.append('send')
                return 0
        api=object.__new__(_Api)
        api.user=User()
        def zero(events):
            try:
                return _Api.send(api,events)
            finally:
                self.api.last_send_win32_error=api.last_send_win32_error
        self.api.send=zero
        with patch('screenqr.native_input.ctypes.set_last_error',
                side_effect=lambda value: order.append(('clear',value))), \
             patch('screenqr.native_input.ctypes.get_last_error',
                side_effect=lambda: order.append('cache') or 5):
            with self.assertRaises(InputOutcomeUnknown):
                self.trigger_once()
        self.assertEqual(order,[('clear',0),'send','cache'])
        report=self.dispatcher.last_report
        self.assertEqual((report.state,report.returned_event_count,report.win32_error),
            ('OUTCOME_UNKNOWN',0,5))
        self.assertEqual(report.operation_send_input_calls,1)
        self.assertEqual(self.dispatcher.metadata_only()['win32_error'],5)
        with self.assertRaises(InputBlocked):
            self.trigger_once()
        self.assertEqual(self.dispatcher.last_report.send_input_calls,1)

    def test_full_instance_change_cannot_select_or_cleanup_a_different_target(self):
        self.select_once()
        other = replace(self.instance,pid=self.instance.pid+1)
        with self.assertRaises(InputBlocked):
            self.dispatcher.cancel_once(other,self.request,self.deadline,lambda:True)
        self.assertEqual(len(self.api.calls),1)
        self.dispatcher.cancel_once(self.instance,self.request,self.deadline,lambda:True)
        self.assertEqual(len(self.api.calls),2)
        self.assertEqual([event.ki.wVk for event in self.api.calls[-1]],[27,27])
        with self.assertRaises(InputBlocked):
            self.dispatcher.cancel_once(self.instance,self.request,self.deadline,lambda:True)
        self.assertEqual(len(self.api.calls),2)

    def test_same_request_operation_can_dispatch_only_once_even_concurrently(self):
        entered, release, second_started, second_done = [threading.Event() for _ in range(4)]
        errors = []
        def send(events):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test barrier expired')
            return len(events)
        self.api.send_hook = send
        def first():
            try: self.trigger_once()
            except Exception as exc: errors.append(type(exc))
        def second():
            second_started.set()
            try: self.trigger_once()
            except Exception as exc: errors.append(type(exc))
            finally: second_done.set()
        a,b = threading.Thread(target=first),threading.Thread(target=second)
        a.start()
        try:
            self.assertTrue(entered.wait(2))
            b.start()
            self.assertTrue(second_started.wait(2))
            self.assertFalse(second_done.wait(0.03))
        finally:
            release.set()
            a.join(2)
            if b.ident is not None: b.join(2)
        self.assertFalse(a.is_alive())
        self.assertFalse(b.is_alive())
        self.assertEqual(errors,[InputBlocked])
        self.assertEqual(len(self.api.calls),1)


if __name__=='__main__':
    unittest.main(verbosity=2)
