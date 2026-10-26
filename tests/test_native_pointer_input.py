"""Permission state, scoped source ABI, and observer lifetime without native GUI."""
import ctypes as C
from dataclasses import FrozenInstanceError
import os
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from livegallery import native_pointer_input as m


def source(kind=16, origin=1):
    return m.SourceEvidence(kind, origin, True, True, True, 'ok')


def packet(device=51, report=b'\x54\x00', count=1):
    body=struct.pack('<II',len(report),count)+report*count
    return struct.pack('<IIQQ',2,24+len(body),device,0)+body


class ScopeTests(unittest.TestCase):
    def test_scope_pair_and_query_are_exactly_once(self):
        calls=[]
        def register(enabled):calls.append(('register',enabled));return True
        def query():calls.append(('query',));return 16,1
        result=m.scoped_source(register,query)
        self.assertEqual(calls,[('register',True),('query',),('register',False)])
        self.assertEqual(result,source())

    def test_query_exception_still_releases_scope(self):
        calls=[]
        def query():raise RuntimeError('failure')
        result=m.scoped_source(lambda v:calls.append(v)or True,query)
        self.assertEqual(calls,[True,False]);self.assertFalse(result.query_succeeded)
        self.assertTrue(result.scope_restored)

    def test_failed_enter_does_not_query_or_unbalance_scope(self):
        calls=[]
        result=m.scoped_source(lambda v:calls.append(v)or False,lambda:self.fail('query'))
        self.assertEqual(calls,[True]);self.assertFalse(result.scope_entered)

    def test_restore_failure_discards_source(self):
        result=m.scoped_source(lambda enabled:enabled,lambda:(2,1))
        self.assertFalse(result.scope_restored);self.assertFalse(result.query_succeeded)
        self.assertIsNone(result.device_type)

    def test_query_unavailable_still_balanced(self):
        calls=[];r=m.scoped_source(lambda e:calls.append(e)or True,lambda:None)
        self.assertEqual(calls,[True,False]);self.assertEqual(r.reason,'source_query_failed')


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.now=10.0;self.p=m.PressPermissions(clock=lambda:self.now)

    def test_real09_single_mouse_press_with_button_up_cannot_play(self):
        # No first mouse click exists in the recorded hardware sequence.
        self.p.update_ptp(51,False)
        token=self.p.press(8,18465390,source())
        self.assertIs(token.allow_hold,False)
        self.assertEqual(token.reason,'mechanical_button_up')
        self.assertIs(self.p.permission_for(8,18465390),token)

    def test_button_down_permits_immediate_mechanical_hold(self):
        self.p.update_ptp(51,True);token=self.p.press(8,100,source())
        self.assertIs(token.allow_hold,True);self.assertTrue(self.p.is_current(token))

    def test_mouse_is_independent_of_concurrent_touchpad_finger(self):
        self.p.update_ptp(51,False);token=self.p.press(8,100,source(2))
        self.assertIs(token.allow_hold,True);self.assertEqual(token.source,'mouse')
        self.p.update_ptp(51,None);self.now+=10
        self.assertTrue(self.p.is_current(token))

    def test_touch_source_does_not_authorize_mouse_hold(self):
        token=self.p.press(8,100,source(4))
        self.assertIsNone(token.allow_hold);self.assertEqual(token.source,'touch')

    def test_missing_stale_and_failed_hid_are_unknown(self):
        for button,age in ((None,0),(True,.251),(False,.251)):
            self.p.cancel();self.p.update_ptp(51,button);self.now+=age
            token=self.p.press(8,self.p.sequence+100,source())
            self.assertIsNone(token.allow_hold)

    def test_exact_freshness_boundary(self):
        self.p.update_ptp(51,False);self.now+=.25
        self.assertIs(self.p.press(8,100,source()).allow_hold,False)

    def test_ambiguous_two_pad_devices_never_grant(self):
        self.p.update_ptp(51,True);self.p.update_ptp(52,False)
        token=self.p.press(8,100,source())
        self.assertIsNone(token.allow_hold);self.assertEqual(token.reason,'ambiguous_touchpad_devices')

    def test_false_and_unknown_cannot_upgrade_with_later_button_down(self):
        for initial in (False,None):
            self.p.cancel();self.p.update_ptp(51,initial)
            token=self.p.press(8,self.p.sequence+100,source())
            self.p.update_ptp(51,True)
            self.assertIs(token.allow_hold,initial)
            with self.assertRaises(FrozenInstanceError):token.allow_hold=True

    def test_current_button_up_or_unknown_revokes_allowed_pad_hold(self):
        for subsequent in (False,None):
            self.p.cancel();self.p.update_ptp(51,True)
            token=self.p.press(8,self.p.sequence+100,source())
            self.p.update_ptp(51,subsequent)
            self.assertFalse(self.p.is_current(token))
            self.p.update_ptp(51,True)
            self.assertFalse(self.p.is_current(token))

    def test_report_gap_or_second_device_cannot_revive_granted_press(self):
        self.p.update_ptp(51,True);token=self.p.press(8,100,source())
        self.now+=.251;self.p.update_ptp(51,True)
        self.assertFalse(self.p.is_current(token))
        token=self.p.press(8,101,source());self.assertTrue(self.p.is_current(token))
        self.p.update_ptp(52,True);self.assertFalse(self.p.is_current(token))
        self.p.update_ptp(51,True);self.assertFalse(self.p.is_current(token))

    def test_failed_parse_cannot_later_revive_granted_press(self):
        self.p.update_ptp(51,True);token=self.p.press(8,100,source())
        self.p.invalidate_ptp();self.p.update_ptp(51,True)
        self.assertFalse(self.p.is_current(token))

    def test_stale_allowed_pad_report_no_longer_grants(self):
        self.p.update_ptp(51,True);token=self.p.press(8,100,source());self.now+=.251
        self.assertFalse(self.p.is_current(token))

    def test_cancel_keeps_invalid_exact_tombstone(self):
        token=self.p.press(8,100,source(2));self.p.cancel()
        self.assertIs(self.p.permission_for(8,100),token)
        self.assertFalse(self.p.is_current(token))
        self.assertEqual(self.p.ptp,{})

    def test_release_keeps_invalid_exact_tombstone(self):
        token=self.p.press(8,100,source(2));self.p.release()
        self.assertIs(self.p.permission_for(8,100),token)
        self.assertFalse(self.p.is_current(token))

    def test_next_press_cannot_reuse_previous_permission(self):
        first=self.p.press(8,100,source(2));second=self.p.press(9,101,source(2))
        self.assertFalse(self.p.is_current(first));self.assertTrue(self.p.is_current(second))

    def test_lookup_matches_hwnd_timestamp_and_button(self):
        token=self.p.press(8,100,source(2))
        for args in ((9,100,1),(8,101,1),(8,100,2)):
            self.assertIsNone(self.p.permission_for(*args))
        self.assertIs(self.p.permission_for(8,100),token)

    def test_duplicate_timestamp_is_ambiguous(self):
        first=self.p.press(8,100,source(2));second=self.p.press(8,100,source(2))
        self.assertIsNone(second.allow_hold);self.assertFalse(self.p.is_current(first))

    def test_scoped_failure_never_treats_mouse_as_physical(self):
        for s in (m.SourceEvidence(),m.SourceEvidence(2,1,False,True,True),
                  m.SourceEvidence(2,1,True,False,True),source(0),source(2,4)):
            self.assertIsNone(self.p.press(8,self.p.sequence+100,s).allow_hold)

    def test_unknown_missing_match_and_validity_are_distinct(self):
        self.assertIsNone(self.p.permission_for(8,100));self.assertFalse(self.p.is_current(None))
        token=self.p.press(8,100,m.SourceEvidence())
        self.assertIsNotNone(self.p.permission_for(8,100));self.assertIsNone(token.allow_hold)

    def test_record_capacity_is_bounded(self):
        for t in range(200):self.p.press(8,t,source(2))
        self.assertEqual(len(self.p.records),128)

    def test_backward_clock_does_not_make_report_fresh(self):
        self.p.update_ptp(51,True);self.now-=1
        self.assertIsNone(self.p.press(8,100,source()).allow_hold)


class FakeBackend:
    def __init__(self):
        self.entries=[];self.calls=[];self.foreground=True;self.own=True
        self.source=source();self.button=False;self.fail=None
    def registrations(self):
        if self.fail=='query':raise OSError('registrations')
        return list(self.entries)
    def register(self,hwnd):
        if self.fail=='register':raise OSError('register')
        self.calls.append(('register',hwnd));self.entries=[(13,5,0,hwnd)]
    def remove(self):
        if self.fail=='remove':raise OSError('unregister')
        self.calls.append('remove');self.entries=[]
    def own_window(self,hwnd):return self.own
    def foreground_is_ours(self):return self.foreground
    def current_source(self):
        self.calls.append('source')
        if self.fail=='source':raise OSError('source')
        return self.source
    def restore_source_scope(self):self.calls.append('restore');return self.fail!='restore'
    def root_window(self,hwnd):return hwnd
    def header(self,handle):self.calls.append('header');return dict(kind=2,size=34,device=51,wparam=0)
    def descriptor(self,device):return {}
    def packet(self,handle,size):return packet()
    def decode(self,descriptor,data):
        if self.fail=='decode':raise ValueError('bad report')
        return dict(button1=self.button,button_status='ok')


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.b=FakeBackend();self.now=10.
        self.m=m.NativePointerInput(backend=self.b,clock=lambda:self.now)
        self.assertTrue(self.m.start())
    def tearDown(self):self.m.close()
    def msg(self,kind,t=100,wparam=0):
        return SimpleNamespace(message=kind,hwnd=8,wParam=wparam,lParam=55,time=t)
    def test_target_null_foreground_registration_and_close(self):
        self.assertEqual(self.b.entries,[(13,5,0,0)]);self.m.close()
        self.assertEqual(self.b.entries,[]);self.assertEqual(self.b.calls.count('remove'),1)
        self.m.close();self.assertEqual(self.b.calls.count('remove'),1)
    def test_existing_registration_not_overwritten(self):
        b=FakeBackend();b.entries=[(13,5,0,99)]
        manager=m.NativePointerInput(backend=b)
        self.assertFalse(manager.start());manager.close()
        self.assertEqual(b.entries,[(13,5,0,99)]);self.assertEqual(b.calls,[])
    def test_failed_registration_still_installs_and_correlates_native_press(self):
        for failure in ('query','register'):
            b=FakeBackend();b.fail=failure;installed=[]
            manager=m.NativePointerInput(backend=b)
            manager.application=SimpleNamespace(
                installNativeEventFilter=lambda f:installed.append('native'),
                installEventFilter=lambda f:installed.append('qt'),
                removeNativeEventFilter=lambda f:None,removeEventFilter=lambda f:None,
                aboutToQuit=SimpleNamespace(connect=lambda f:None))
            self.assertFalse(manager.start());self.assertTrue(manager.started)
            self.assertEqual(installed,['native','qt'])
            manager.observe_message(self.msg(m.WM_LBUTTONDOWN))
            token=manager.permission_for(8,100)
            self.assertIsNotNone(token);self.assertIsNone(token.allow_hold)
            manager.close()
    def test_source_query_failure_uses_root_hwnd_for_tombstone(self):
        self.b.root_window=lambda hwnd:88;self.b.fail='source'
        self.m.observe_message(self.msg(m.WM_LBUTTONDOWN))
        token=self.m.permission_for(88,100)
        self.assertIsNotNone(token);self.assertIsNone(token.allow_hold)
        self.assertEqual(token.reason,'native_query_error')
    def test_changed_registration_not_removed(self):
        self.b.entries=[(13,5,0,99)];self.m.close()
        self.assertEqual(self.b.entries,[(13,5,0,99)])
    def test_close_failure_remains_visible(self):
        self.b.fail='remove';self.m.close()
        self.assertTrue(self.m.registered);self.assertEqual(self.m.summary()['registration_status'],'unregister_failed')
    def test_raw_report_false_then_exact_native_press_is_denied(self):
        self.m.observe_message(self.msg(m.WM_INPUT));self.m.observe_message(self.msg(m.WM_LBUTTONDOWN))
        auth=self.m.authorize(SimpleNamespace(timestamp=lambda:100,button=lambda:1),8)
        self.assertIs(auth.allow_hold,False);self.assertEqual(self.m.counters['reports'],1)
    def test_source_is_queried_only_on_left_press(self):
        self.m.observe_message(self.msg(m.WM_INPUT));self.m.observe_message(self.msg(m.WM_LBUTTONUP))
        self.m.observe_message(self.msg(0x200));self.assertNotIn('source',self.b.calls)
        self.m.observe_message(self.msg(m.WM_LBUTTONDOWN));self.assertEqual(self.b.calls.count('source'),1)
    def test_foreign_or_background_input_is_not_parsed(self):
        self.b.own=False;self.m.observe_message(self.msg(m.WM_INPUT))
        self.b.own=True;self.b.foreground=False;self.m.observe_message(self.msg(m.WM_INPUT))
        self.b.foreground=True;self.m.observe_message(self.msg(m.WM_INPUT,wparam=1))
        self.assertNotIn('header',self.b.calls)
    def test_background_cancels_token_and_hid_state(self):
        self.b.source=source(2);self.m.observe_message(self.msg(m.WM_LBUTTONDOWN))
        auth=self.m.permission_for(8,100);self.b.foreground=False
        self.m.observe_message(self.msg(m.WM_INPUT));self.assertFalse(self.m.is_current(auth))
        self.assertIs(self.m.permission_for(8,100),auth)
    def test_parse_failure_invalidates_allowed_pad_token(self):
        self.b.button=True;self.m.observe_message(self.msg(m.WM_INPUT))
        self.m.observe_message(self.msg(m.WM_LBUTTONDOWN));auth=self.m.permission_for(8,100)
        self.assertTrue(self.m.is_current(auth));self.b.fail='decode'
        self.m.observe_message(self.msg(m.WM_INPUT));self.assertFalse(self.m.is_current(auth))
    def test_failed_scope_is_unknown_and_not_reentered(self):
        self.b.source=m.SourceEvidence(scope_entered=True,scope_restored=False)
        self.m.observe_message(self.msg(m.WM_LBUTTONDOWN));auth=self.m.permission_for(8,100)
        self.assertIsNone(auth.allow_hold);self.assertFalse(self.m.scope_healthy)
        self.m.observe_message(self.msg(m.WM_LBUTTONDOWN,101));self.assertEqual(self.b.calls.count('source'),1)
        self.assertEqual(self.m.scope_debt,1);self.m.close()
        self.assertEqual(self.b.calls.count('restore'),1);self.assertEqual(self.m.scope_debt,0)
        self.assertEqual(self.m.summary()['scope_cleanup_status'],'restored')
        self.m.close();self.assertEqual(self.b.calls.count('restore'),1)
    def test_failed_scope_cleanup_keeps_debt_visible(self):
        self.b.source=m.SourceEvidence(scope_entered=True,scope_restored=False)
        self.m.observe_message(self.msg(m.WM_LBUTTONDOWN));self.b.fail='restore';self.m.close()
        self.assertEqual(self.m.scope_debt,1)
        self.assertEqual(self.m.summary()['scope_cleanup_status'],'restore_failed')
        self.m.close();self.assertEqual(self.b.calls.count('restore'),1)
    def test_scope_cleanup_cannot_decrement_another_thread(self):
        self.b.source=m.SourceEvidence(scope_entered=True,scope_restored=False)
        self.m.observe_message(self.msg(m.WM_LBUTTONDOWN))
        with patch.object(m.threading,'get_ident',return_value=-1):self.m.close()
        self.assertNotIn('restore',self.b.calls);self.assertEqual(self.m.scope_debt,1)
        self.assertEqual(self.m.summary()['scope_cleanup_status'],'wrong_thread_left_untouched')
    def test_summary_is_read_only_no_native_queries(self):
        before=list(self.b.calls);self.m.diagnostic_snapshot();self.m.summary()
        self.assertEqual(self.b.calls,before)


class AbiAndPacketTests(unittest.TestCase):
    def test_x64_abi(self):
        self.assertEqual(m.abi_sizes(),{'RAWINPUTDEVICE':16,'RAWINPUTHEADER':24,'DEVICEINFO':32,
            'HIDP_CAPS':64,'HIDP_BUTTON_CAPS':72,'HIDP_VALUE_CAPS':72})
        self.assertEqual(C.sizeof(m.INPUT_MESSAGE_SOURCE),8)
        self.assertEqual(m.HIDP_VALUE_CAPS.Usages.offset,56)
    def test_valid_and_bounded_packet(self):
        self.assertEqual(m.split_hid_packet(packet()),(51,[b'\x54\x00']))
        for data in (packet()[:-1],packet()+b'x',b'x'*(m.MAX_PACKET+1)):
            with self.assertRaises(ValueError):m.split_hid_packet(data)
        data=bytearray(packet());struct.pack_into('<II',data,24,0xffffffff,0xffffffff)
        with self.assertRaises(ValueError):m.split_hid_packet(bytes(data))
    def test_wrong_report_cannot_mean_button_up(self):
        api=m._WinApi.__new__(m._WinApi)
        descriptor=dict(report_bytes=29,button_reports={84},contact_reports={84})
        result=api.decode(descriptor,b'\x11\x00')
        self.assertIsNone(result['button1']);self.assertEqual(result['button_status'],'not_in_report')
    def test_no_windows_gui_singleton_on_offscreen(self):
        app=SimpleNamespace(platformName=lambda:'offscreen')
        self.assertIsNone(m.NativePointerInput.ensure_for_application(app))
        self.assertIsNone(m.ensure_for_application(app))


if __name__=='__main__':unittest.main()
