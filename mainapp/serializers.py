from .models import PasskeyCredential, Role, User, Workspace, WorkspaceInvite
from .service.catalog_service import serialize_workspace_catalog_summary
from .service.role_service import (
    compute_user_permissions_version,
    count_active_seats,
    is_workspace_owner,
    normalize_role_field_rights,
    normalize_role_object_rights,
    user_can_manage_sales_settings,
    user_can_manage_users_and_roles,
    user_can_manage_workspace_catalog,
)


def serialize_user_role_summary(role: Role | None, *, is_owner_fallback: bool = False) -> dict:
    """Serialize a compact role summary for user, invite, and member payloads."""
    if role is not None:
        return {
            'id': role.id,
            'name': role.name,
            'is_owner': bool(role.is_owner),
            'is_default_staff': bool(role.is_default_staff),
        }
    return {
        'id': None,
        'name': 'Owner' if is_owner_fallback else 'Staff',
        'is_owner': is_owner_fallback,
        'is_default_staff': not is_owner_fallback,
    }


def serialize_role(
    role: Role,
    *,
    member_count: int | None = None,
    active_member_count: int | None = None,
    shared_record_count: int | None = None,
) -> dict:
    """Serialize a workspace Role including its capabilities, switches, object rights, and field rights."""
    data = {
        'id': role.id,
        'name': role.name,
        'is_owner': bool(role.is_owner),
        'is_default_staff': bool(role.is_default_staff),
        'is_removable': not bool(role.is_owner or role.is_default_staff),
        'is_deactivatable': not bool(role.is_owner),
        'description': role.description,
        'can_manage_seat_plan': bool(role.can_manage_seat_plan),
        'can_manage_sms_wallet': bool(role.can_manage_sms_wallet),
        'switches': role.switches_dict(),
        'object_rights': normalize_role_object_rights(role),
        'field_rights': normalize_role_field_rights(role),
        'created_at': role.created_at.isoformat(),
    }
    if member_count is not None:
        data['member_count'] = member_count
    if active_member_count is not None:
        data['active_member_count'] = active_member_count
    if shared_record_count is not None:
        data['shared_record_count'] = shared_record_count
    return data


def serialize_workspace_and_user(workspace: Workspace, user: User) -> dict:
    """Serialize authenticated session context with workspace, catalog, user, role, and permissions."""
    is_owner = is_workspace_owner(user, workspace)
    can_manage_users = user_can_manage_users_and_roles(user, workspace)
    can_manage_catalog = user_can_manage_workspace_catalog(user, workspace)
    can_manage_sales = user_can_manage_sales_settings(user, workspace)
    role = getattr(user, 'role', None)
    role_summary = serialize_user_role_summary(role, is_owner_fallback=is_owner)
    switches = (
        role.switches_dict()
        if role is not None
        else {
            'import': is_owner,
            'export': is_owner,
            'automations': is_owner,
            'scripts': is_owner,
            'users_and_roles': is_owner,
            'theme_and_layouts': is_owner,
            'unattended_agent': is_owner,
        }
    )
    permissions_version = compute_user_permissions_version(user, workspace)

    return {
        'workspace': {
            'name': workspace.name,
            'slug': workspace.slug,
            **serialize_workspace_catalog_summary(workspace),
        },
        'user': {
            'email': user.email,
            'timezone': user.timezone,
            'email_verified': bool(user.email_verified),
        },
        'active_seat_count': count_active_seats(workspace),
        'role': role_summary,
        'permissions_version': permissions_version,
        'permissions': {
            'is_owner': is_owner,
            'can_manage_invites': can_manage_users,
            'can_manage_users_and_roles': can_manage_users,
            'can_manage_packs': can_manage_catalog,
            'can_manage_workspace_profile': can_manage_catalog,
            'can_manage_sales_settings': can_manage_sales,
            'can_manage_seat_plan': is_owner,
            'can_manage_sms_wallet': is_owner,
            'role_id': role_summary['id'],
            'role_name': role_summary['name'],
            'switches': switches,
            'object_rights': normalize_role_object_rights(role, is_owner_fallback=is_owner),
            'field_rights': normalize_role_field_rights(role, is_owner_fallback=is_owner),
            'permissions_version': permissions_version,
        },
        'mailbox_connection': {
            'connected': False,
            'provider': None,
        },
    }


def serialize_member(member: User, workspace: Workspace) -> dict:
    """Serialize a workspace member row for the People directory."""
    is_owner = is_workspace_owner(member, workspace)
    role = getattr(member, 'role', None)
    return {
        'id': member.id,
        'email': member.email,
        'timezone': member.timezone,
        'email_verified': bool(member.email_verified),
        'is_active': bool(member.is_active),
        'is_owner': is_owner,
        'is_removable': not is_owner,
        'is_deactivatable': not is_owner,
        'counts_as_active_seat': bool(member.is_active and member.email_verified),
        'role': serialize_user_role_summary(role, is_owner_fallback=is_owner),
        'created_at': member.created_at.isoformat(),
    }


def serialize_invite(invite: WorkspaceInvite, now=None) -> dict:
    """Serialize a workspace invitation row including its assigned role."""
    role = getattr(invite, 'role', None)
    return {
        'id': invite.id,
        'email': invite.email,
        'role': serialize_user_role_summary(role, is_owner_fallback=False),
        'status': invite.current_status(now=now),
        'invited_by_email': invite.invited_by.email if invite.invited_by else None,
        'last_sent_at': invite.last_sent_at.isoformat() if invite.last_sent_at else None,
        'expires_at': invite.expires_at.isoformat(),
        'accepted_at': invite.accepted_at.isoformat() if invite.accepted_at else None,
        'revoked_at': invite.revoked_at.isoformat() if invite.revoked_at else None,
        'created_at': invite.created_at.isoformat(),
    }


def serialize_passkey(passkey: PasskeyCredential) -> dict:
    """Serialize a registered passkey credential summary."""
    return {
        'id': passkey.id,
        'name': passkey.name,
        'created_at': passkey.created_at.isoformat(),
        'last_used_at': passkey.last_used_at.isoformat() if passkey.last_used_at else None,
        'backed_up': bool(passkey.backed_up),
    }
