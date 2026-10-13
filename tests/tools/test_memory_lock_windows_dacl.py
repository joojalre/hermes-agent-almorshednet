"""Native DACL evidence for MemoryStore locks created in disposable Windows parents.

This tests inheritance and the security verifier, not remediation of old loose locks.
"""

import pytest

from hermes_cli import windows_ssh_runtime as security
from tools.memory_tool import MemoryStore

pytestmark = pytest.mark.platforms("windows")


def _parent(tmp_path, *, everyone=False):
    w = security._win32()
    attributes = security._security_attributes()
    descriptor = attributes.SECURITY_DESCRIPTOR
    dacl = w.win32security.ACL()
    flags = w.win32con.OBJECT_INHERIT_ACE | w.win32con.CONTAINER_INHERIT_ACE
    principals = [security._current_sid(), security._system_sid()]
    if everyone:
        principals.append(w.win32security.ConvertStringSidToSid("S-1-1-0"))
    for sid in principals:
        dacl.AddAccessAllowedAceEx(w.win32security.ACL_REVISION, flags, w.ntsecuritycon.FILE_ALL_ACCESS, sid)
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    path = tmp_path / "synthetic-parent"
    w.win32file.CreateDirectory(str(path), attributes)
    return path


def _lock_handle(path):
    w = security._win32()
    return w.win32file.CreateFile(
        str(path), w.win32con.READ_CONTROL, w.win32con.FILE_SHARE_READ | w.win32con.FILE_SHARE_WRITE,
        None, w.win32con.OPEN_EXISTING, w.win32con.FILE_ATTRIBUTE_NORMAL, None,
    )


def test_memory_lock_inherits_owner_and_system_only(tmp_path):
    parent = _parent(tmp_path)
    with MemoryStore._file_lock(parent / "MEMORY.md"):
        pass
    w = security._win32()
    handle = _lock_handle(parent / "MEMORY.md.lock")
    try:
        security._verify_security(handle)
        descriptor = w.win32security.GetSecurityInfo(
            handle, w.win32security.SE_FILE_OBJECT, w.win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        aces = [dacl.GetAce(index) for index in range(dacl.GetAceCount())]
        assert {security._sid_str(ace[-1]) for ace in aces} == security._allowed_sids()
        assert all(ace[0][1] & w.win32security.INHERITED_ACE for ace in aces)
    finally:
        w.win32file.CloseHandle(handle)


def test_memory_lock_with_inherited_everyone_grant_is_rejected_by_verifier(tmp_path):
    parent = _parent(tmp_path, everyone=True)
    with MemoryStore._file_lock(parent / "MEMORY.md"):
        pass
    handle = _lock_handle(parent / "MEMORY.md.lock")
    try:
        with pytest.raises(OSError, match="permissive DACL"):
            security._verify_security(handle)
    finally:
        security._win32().win32file.CloseHandle(handle)
