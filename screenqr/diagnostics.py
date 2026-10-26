"""Opt-in last-result diagnostics: fixed internal codes, never screen contents."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from . import __version__
from .native_job import _FAILURE_DETAILS, _OPERATION_LABELS, NativeJobResult

_CODES = frozenset(_FAILURE_DETAILS) | frozenset(_OPERATION_LABELS) | frozenset({
    'single_commit_dispatch_returned_business_result_unconfirmed',
    'selection_evidence_identity_or_interval_invalid',
    'selection_evidence_replayed_or_overlapping',
    'native_display_not_verified_after_complete_samples',
    'strict_native_selection_evidence_rejected',
    'native_toolbar_not_verified_after_complete_samples',
    'native_job_failed', 'native_operation_watchdog_expired',
    'not_attempted', 'dispatch_returned', 'exact_calibrated_profile_two_quarters_agree',
    'unique_original_structure_only_no_input_permission',
    'stage_validation_rejected', 'boundary_rejected_other', 'boundary_receipt_rejected',
    'selection_wait_sample_incomplete_or_changed', 'selection_not_verified_after_complete_samples',
    'actual_selection_bbox_differs_from_requested',
    'own_stage_foreground_rejected', 'own_stage_not_ready',
    'login_requires_camera_scan',
    'cleanup_deadline_expired', 'cleanup_probe_rejected_or_ambiguous',
    'cleanup_postcommit_absence_not_stable', 'cleanup_bound_instance_changed',
    'cleanup_foreground_left_allowed_windows', 'cleanup_probe_or_dispatch_failed',
    'cleanup_absent_postcommit_readonly', 'cleanup_wait_bound_postcommit_readonly',
    'cleanup_wait_known_transition', 'cleanup_absent_own_foreground',
    'cleanup_bound_native_ready',
    'port_error:ValueError', 'port_error:TypeError', 'port_error:StageValidationError',
    'port_error:OSError', 'port_error:RuntimeError',
})
_STATUS = frozenset({'CANCELLED','TARGET_NOT_CONFIRMED','MULTIPLE_CODES','TIMED_OUT',
    'OUTCOME_UNKNOWN','CONTENT_UNPROVEN','UNSUPPORTED_BUSINESS','FRAME_TOO_LARGE',
    'FAILED','REJECTED','DISPATCHED_UNCONFIRMED','PREPARATION_STOPPED'})
_CLEANUP = frozenset({'NOT_NEEDED','CONFIRMED_ABSENT','CONFIRMED_CLOSED','UNKNOWN',
    'FAILED','UNBOUND_OUTCOME_UNKNOWN'})
_FIELDS = frozenset({'job_phase','last_operation','last_stage_error_code',
    'last_native_error_code','last_selection_error_code','last_selection_boundary_reason',
    'last_input_error_code','last_toolbar_reason_code','last_cleanup_error_code','last_cleanup_probe_code'})

def _code(value):
    return value if isinstance(value,str) and value in _CODES else (None if value is None else 'other_internal_code')

class NativeDiagnostics:
    """Explicit developer path; overwrite one result, fail silently on I/O errors."""
    def __init__(self,path: str | Path):
        self.path=Path(path)

    def _write(self,data: dict) -> None:
        temporary=self.path.with_name('.'+self.path.name+'.'+uuid4().hex+'.tmp')
        try:
            with temporary.open('x',encoding='utf-8') as output:
                json.dump({'version':__version__,'pixels_saved':False,
                    'QR_text_saved':False,'business_success':False,**data},output,ensure_ascii=True)
            os.replace(temporary,self.path)
        except OSError:
            pass
        finally:
            try:temporary.unlink(missing_ok=True)
            except OSError:pass

    def preparation(self,code: str) -> None:
        self._write({'status':'PREPARATION_STOPPED','reason':_code(code)})

    def result(self,result: NativeJobResult) -> None:
        self._write({'status':result.status if result.status in _STATUS else 'REJECTED',
            'reason':_code(result.reason),'dispatch_attempted':result.dispatch_attempted is True,
            'dispatch_count':result.dispatch_count if type(result.dispatch_count) is int and 0<=result.dispatch_count<=1 else None,
            'cleanup_status':result.cleanup_status if result.cleanup_status in _CLEANUP else None,
            'diagnostics':{key:_code(value) for key,value in result.diagnostics if key in _FIELDS}})
