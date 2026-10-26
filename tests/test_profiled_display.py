"""Pure synthetic A-contract checks; not real-machine or business acceptance.

No screenshots, GUI, native input or private fixture is used. Adapter metadata is
synthetic; these tests prove rejection/binding logic, never real acquisition.
"""
import copy
from dataclasses import replace
import hashlib
import unittest
from uuid import uuid4

from test_controller import fixture, Clock
from screenqr.handoff import (AbsenceReceipt, CaptureReceipt, CommitCheck, FrameBinding,
    GateRejected, HandoffSession, NativeSnapshot, ObservationReceipt, Policy,
    SelectionExtentReceipt, SelectionReceipt, StageReceipt, ProfiledDisplayReceipt,
    EXACT_SOURCE_RGB, NATIVE_DISPLAY_PROFILE_A, PROFILE_A_ID, NativeDisplayPredictionMismatch)


class ProfiledDisplayTests(unittest.TestCase):
    def setup_bound_capture(self, mode=NATIVE_DISPLAY_PROFILE_A):
        self.clock = Clock()
        self.frame, verification, self.probe = fixture()
        self.policy = Policy(10_000, 1_000, 100, 50, selection_evidence_mode=mode)
        self.session = HandoffSession(self.frame, policy=self.policy, clock=self.clock)
        self.stage = StageReceipt.from_stage_verification(self.frame,
            replace(verification, checked_monotonic_ns=self.clock()))
        self.session.bind_stage(self.stage)
        absent = AbsenceReceipt.from_probe(self.session.request_id,
            dict(status='NOT_FOUND', candidates=[], errors=[]),
            started_ns=self.clock(), finished_ns=self.clock())
        self.session.record_absence(absent)
        trigger = self.session.mark_trigger_attempt()
        self.snapshot = NativeSnapshot.from_probe(self.probe, started_ns=self.clock(),
            finished_ns=self.clock(), fixed_file_version=(4, 1, 15, 13))
        self.session.bind_capture(CaptureReceipt(self.session.request_id, absent.evidence_id, trigger, self.snapshot))
        start, pixel_start, pixel_end, completed, finish = [self.clock() for _ in range(5)]
        predicted = bytes(max(v-1, 0) for v in self.frame.rgb8)
        self.evidence = dict(schema_version=1, status='PAYLOAD_MISMATCH', errors=[], read_only=True,
            native_input_sent=False, raw_pixels_persisted=False, frame_id=self.frame.frame_id,
            stage_id=self.stage.stage_id, stage_epoch=self.stage.stage_epoch,
            generation=self.frame.generation, source_capture_sha256=self.frame.capture_sha256,
            topology_sha256=self.frame.topology_sha256, session_id=self.session.request_id,
            evidence_id=str(uuid4()), started_monotonic_ns=start, capture_completed_monotonic_ns=completed,
            finished_monotonic_ns=finish, deadline_monotonic_ns=start+2_000_000_000,
            version_metadata=dict(fixed_file_version=[4,1,15,13], expected_fixed_file_version=[4,1,15,13]),
            native_identity_before=copy.deepcopy(self.probe), native_identity_after=copy.deepcopy(self.probe),
            payload_comparison=dict(captured_rgb_sha256=hashlib.sha256(predicted).hexdigest(),
                expected_rgb_sha256=self.frame.capture_sha256, width=self.frame.width, height=self.frame.height,
                captured_bbox_physical=self.stage.payload_bbox_physical, exact_rgb_bytes_equal=False,
                capture_started_monotonic_ns=pixel_start, capture_ended_monotonic_ns=pixel_end))
        # The independent synthetic boundary producer supplies this separate
        # schema. Production adapters must actually observe edges, never echo a drag.
        self.extent_evidence = copy.deepcopy(self.evidence)
        self.extent_evidence.update(selection_extent_status='VERIFIED',
            observed_selection_bbox_physical=self.stage.transport_bbox_physical,
            extent_verification_method='native-client-observed-boundaries-v1')
        self.extent = SelectionExtentReceipt.from_native_extent_evidence(stage=self.stage,
            native_instance=self.snapshot.instance, evidence=self.extent_evidence)

    def receipt(self, evidence=None, **kwargs):
        return ProfiledDisplayReceipt.from_native_display_evidence(frame=self.frame, stage=self.stage,
            extent=self.extent, native_instance=self.snapshot.instance,
            evidence=self.evidence if evidence is None else evidence, clock=self.clock, **kwargs)

    def test_forward_prediction_keeps_source_unchanged_and_never_claims_raw(self):
        self.setup_bound_capture()
        original, original_hash = self.frame.rgb8, self.frame.capture_sha256
        receipt = self.receipt()
        self.session.bind_selection(receipt)
        self.assertIs(self.frame.rgb8, original)
        self.assertEqual(hashlib.sha256(self.frame.rgb8).hexdigest(), original_hash)
        self.assertEqual(receipt.expected_display_sha256, receipt.observed_display_sha256)
        self.assertNotEqual(receipt.observed_display_sha256, self.frame.capture_sha256)
        self.assertFalse(receipt.raw_payload_verified)
        self.assertIn('non-injective', receipt.evidence_scope)
        self.assertEqual(self.session.snapshot()['selection_evidence_mode'], NATIVE_DISPLAY_PROFILE_A)
        self.assertEqual(self.session.snapshot()['state'], 'SELECTION_VERIFIED')

    def test_wrong_content_and_diagnostic_booleans_do_not_authorize(self):
        self.setup_bound_capture()
        evidence = copy.deepcopy(self.evidence)
        evidence['payload_comparison']['captured_rgb_sha256'] = '0'*64
        evidence['diagnostic_only'] = False
        evidence['success'] = evidence['raw_payload_verified'] = True
        with self.assertRaises(NativeDisplayPredictionMismatch):
            self.receipt(evidence)

    def test_wrong_metadata_is_never_the_waitable_prediction_exception(self):
        mutations = (
            lambda e: e['version_metadata'].update(fixed_file_version=[4,1,15,14]),
            lambda e: e['native_identity_after']['candidates'][0].update(pid=701),
            lambda e: e['payload_comparison'].update(width=99),
            lambda e: e.update(stage_epoch=99),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.setup_bound_capture()
                evidence = copy.deepcopy(self.evidence)
                evidence['payload_comparison']['captured_rgb_sha256'] = '0'*64
                mutate(evidence)
                with self.assertRaises(ValueError) as raised:
                    self.receipt(evidence)
                self.assertNotIsInstance(raised.exception, NativeDisplayPredictionMismatch)

    def test_final_clock_expiry_is_checked_before_waitable_hash_mismatch(self):
        self.setup_bound_capture()
        evidence = copy.deepcopy(self.evidence)
        evidence['payload_comparison']['captured_rgb_sha256'] = '0'*64
        ticks = iter((evidence['finished_monotonic_ns']+1, evidence['deadline_monotonic_ns']))
        with self.assertRaises(ValueError) as raised:
            ProfiledDisplayReceipt.from_native_display_evidence(frame=self.frame, stage=self.stage,
                extent=self.extent, native_instance=self.snapshot.instance,
                evidence=evidence, clock=lambda: next(ticks))
        self.assertNotIsInstance(raised.exception, NativeDisplayPredictionMismatch)
        self.assertIn('expired', str(raised.exception))

    def test_unknown_profile_or_native_version_or_dpi_rejected(self):
        self.setup_bound_capture()
        with self.assertRaises(ValueError):
            self.receipt(profile_id='unregistered-display-profile')
        evidence = copy.deepcopy(self.evidence)
        evidence['version_metadata']['fixed_file_version'][-1] += 1
        with self.assertRaises(ValueError):
            self.receipt(evidence)
        evidence = copy.deepcopy(self.evidence)
        evidence['native_identity_after']['candidates'][0]['dpi'] = 96
        with self.assertRaises(ValueError):
            self.receipt(evidence)
        with self.assertRaises(ValueError):
            replace(self.policy, expected_dpi=96)

    def test_wrong_instance_payload_bbox_or_dimensions_rejected(self):
        for change in ('instance', 'bbox', 'size'):
            with self.subTest(change=change):
                self.setup_bound_capture()
                evidence = copy.deepcopy(self.evidence)
                if change == 'instance':
                    evidence['native_identity_after']['candidates'][0]['pid'] += 1
                elif change == 'bbox':
                    p = self.stage.payload_bbox_physical
                    evidence['payload_comparison']['captured_bbox_physical'] = (p[0]+1,p[1],p[2]+1,p[3])
                else:
                    evidence['payload_comparison']['width'] += 1
                with self.assertRaises(ValueError):
                    self.receipt(evidence)

    def test_old_epoch_generation_request_or_observation_rejected(self):
        for field in ('stage_epoch', 'generation', 'session_id', 'evidence_id'):
            with self.subTest(field=field):
                self.setup_bound_capture()
                evidence = copy.deepcopy(self.evidence)
                evidence[field] = evidence[field]+1 if field in ('stage_epoch','generation') else str(uuid4())
                with self.assertRaises(ValueError):
                    self.receipt(evidence)

    def test_expired_capture_factory_and_stale_session_binding_rejected(self):
        self.setup_bound_capture()
        self.clock.value = self.evidence['deadline_monotonic_ns']
        with self.assertRaises(ValueError):
            self.receipt()
        self.setup_bound_capture()
        receipt = self.receipt()
        self.clock.value += self.policy.max_evidence_age_ns+1
        with self.assertRaises(GateRejected):
            self.session.bind_selection(receipt)

    def test_default_legacy_mode_rejects_A_receipt_and_raw_factory_still_rejects_rendered_hash(self):
        self.setup_bound_capture(EXACT_SOURCE_RGB)
        self.assertEqual(Policy(10_000,1_000,100,50).selection_evidence_mode, EXACT_SOURCE_RGB)
        with self.assertRaises(ValueError):
            SelectionReceipt.from_native_selection_evidence(stage=self.stage,
                native_instance=self.snapshot.instance,evidence=self.extent_evidence)
        receipt = self.receipt()
        with self.assertRaises(GateRejected):
            self.session.bind_selection(receipt)

    def test_independent_extent_and_factory_proof_cannot_be_replaced_by_caller_flags(self):
        self.setup_bound_capture()
        with self.assertRaises(ValueError):
            ProfiledDisplayReceipt.from_native_display_evidence(frame=self.frame,stage=self.stage,
                extent=replace(self.extent,_observed_boundary_proof=None),native_instance=self.snapshot.instance,
                evidence=self.evidence,clock=self.clock)
        receipt = replace(self.receipt(),_profiled_display_proof=None)
        with self.assertRaises(GateRejected):
            self.session.bind_selection(receipt)

    def latest_commit_check(self, receipt):
        for index in range(3):
            start, finish = self.clock(), self.clock()
            visual = ObservationReceipt.from_native_observation(dict(read_only=True, errors=[],
                status='READ_ONLY_CANDIDATE', session_id=self.session.request_id,generation=self.frame.generation,
                started_ns=start,finished_ns=finish,version_metadata=dict(fixed_file_version=[4,1,15,13]),
                identity_before=self.probe,identity_after=self.probe,
                selection_bbox_physical=self.stage.transport_bbox_physical,
                candidate_bbox_physical=(200,200,220,220)),observation_id=index)
            self.session.observe(visual)
        native = NativeSnapshot.from_probe(self.probe,started_ns=self.clock(),finished_ns=self.clock(),
                                           fixed_file_version=(4,1,15,13))
        return CommitCheck(self.session.request_id,FrameBinding.from_frame(self.frame),self.stage.stage_id,
            self.stage.stage_epoch,self.stage.transport_bbox_physical,self.frame.generation,
            self.frame.topology_sha256,receipt,visual,native,visual.candidate_bbox_physical)

    def test_existing_commit_gate_accepts_fresh_A_latest_selection_without_raw_claim(self):
        self.setup_bound_capture()
        receipt = self.receipt()
        self.session.bind_selection(receipt)
        check = self.latest_commit_check(receipt)
        permit = self.session.consume_commit_once(check)
        self.assertEqual(permit.request_id,self.session.request_id)
        self.assertFalse(receipt.raw_payload_verified)
        self.assertEqual(self.session.snapshot()['commit_attempts'],1)
        with self.assertRaises(GateRejected):
            self.session.consume_commit_once(check)

    def test_commit_latest_selection_rechecks_A_proof(self):
        self.setup_bound_capture()
        receipt = self.receipt()
        self.session.bind_selection(receipt)
        check = self.latest_commit_check(receipt)
        check = replace(check,latest_selection=replace(receipt,_profiled_display_proof=None))
        with self.assertRaises(GateRejected):
            self.session.consume_commit_once(check)
        self.assertEqual(self.session.snapshot()['commit_attempts'],0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
