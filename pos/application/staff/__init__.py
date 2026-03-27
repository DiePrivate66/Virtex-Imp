"""Staff use cases."""

from .commands import StaffError, register_attendance, save_employee, sync_employee_user
from .permissions import ALLOWED_POS_GROUPS, ensure_pos_groups_for_user, user_is_pos_operator
from .queries import find_employee_by_id, find_employee_by_pin, get_employee_list, normalize_identity_document

__all__ = [
    'ALLOWED_POS_GROUPS',
    'StaffError',
    'ensure_pos_groups_for_user',
    'find_employee_by_id',
    'find_employee_by_pin',
    'get_employee_list',
    'normalize_identity_document',
    'register_attendance',
    'save_employee',
    'sync_employee_user',
    'user_is_pos_operator',
]
