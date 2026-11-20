from django.contrib.auth import login, logout
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from ..helpers.request_helpers import (
    EMAIL_SERVICE_UNAVAILABLE_MESSAGE,
    parse_email_field,
    parse_json_object,
    parse_timezone_field,
    require_workspace_member,
    validate_new_password,
    validate_password_basics,
)
from ..models import AuditLog, Role, User, WorkspaceInvite
from ..serializers import (
    serialize_invite,
    serialize_member,
    serialize_user_role_summary,
    serialize_workspace_and_user,
)
from ..service.audit_service import record_audit_event
from ..service.email_service import (
    EmailDeliveryError,
    EmailThrottledError,
    hash_token,
    send_workspace_invite_email,
)
from ..service.role_service import (
    count_active_seats,
    ensure_workspace_default_roles,
    user_can_manage_users_and_roles,
)

SINGLE_WORKSPACE_ERROR_MESSAGE = (
    "This email address already belongs to a workspace and cannot create or join a second workspace."
)


def user_can_manage_invites(user, workspace) -> bool:
    """Allow the workspace Owner or a role with users_and_roles to manage invites."""
    return user_can_manage_users_and_roles(user, workspace)


@require_GET
def workspace_people_view(request, slug):
    """List workspace members, assignable non-Owner roles, and (for managers) workspace invites."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    ensure_workspace_default_roles(workspace)
    can_manage = user_can_manage_users_and_roles(user, workspace)
    now = timezone.now()
    members = [
        serialize_member(member, workspace)
        for member in workspace.users.select_related("role").order_by("created_at", "id")
    ]
    invites = (
        [
            serialize_invite(invite, now=now)
            for invite in workspace.invites.select_related("invited_by", "role").order_by(
                "-created_at", "-id"
            )
        ]
        if can_manage
        else []
    )
    assignable_roles = [
        serialize_user_role_summary(role)
        for role in workspace.roles.filter(is_owner=False).order_by(
            "-is_default_staff", "name", "id"
        )
    ]
    return JsonResponse(
        {
            "workspace": {
                "name": workspace.name,
                "slug": workspace.slug,
            },
            "active_seat_count": count_active_seats(workspace),
            "can_manage_invites": can_manage,
            "can_manage_users_and_roles": can_manage,
            "assignable_roles": assignable_roles,
            "members": members,
            "invites": invites,
        },
        status=200,
    )


@require_http_methods(["POST"])
def workspace_invite_create_view(request, slug):
    """Send or resend an email invite into this workspace with an optional non-Owner role."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    if not user_can_manage_users_and_roles(user, workspace):
        return JsonResponse(
            {
                "error": (
                    "Only the workspace Owner or a role with users-and-roles "
                    "access can send invites."
                )
            },
            status=403,
        )

    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    email, email_error = parse_email_field(payload.get("email"))
    if email_error is not None:
        return email_error

    raw_role_id = payload.get("role_id")
    parsed_role_id = None
    if raw_role_id is not None and raw_role_id != "":
        if isinstance(raw_role_id, bool):
            return JsonResponse({"error": "role_id must be a valid role ID."}, status=400)
        try:
            parsed_role_id = int(raw_role_id)
        except (TypeError, ValueError):
            return JsonResponse({"error": "role_id must be a valid role ID."}, status=400)
        if parsed_role_id < 1:
            return JsonResponse({"error": "role_id must be a valid role ID."}, status=400)

    existing_user = (
        User.objects.select_related("workspace")
        .filter(email__iexact=email)
        .first()
    )
    if existing_user is not None:
        if existing_user.workspace_id == workspace.id:
            return JsonResponse(
                {"error": "This email address already belongs to a member of this workspace."},
                status=400,
            )
        return JsonResponse(
            {"error": SINGLE_WORKSPACE_ERROR_MESSAGE},
            status=409,
        )

    try:
        with transaction.atomic():
            _owner_role, staff_role = ensure_workspace_default_roles(workspace)
            target_role = staff_role
            if parsed_role_id is not None:
                target_role = Role.objects.filter(
                    id=parsed_role_id, workspace=workspace
                ).first()
                if target_role is None:
                    return JsonResponse(
                        {"error": "Selected role was not found in this workspace."},
                        status=400,
                    )
                if target_role.is_owner:
                    return JsonResponse(
                        {"error": "The Owner role cannot be assigned on an invite."},
                        status=403,
                    )

            invite, _raw_token = send_workspace_invite_email(
                workspace,
                user,
                email,
                role=target_role,
                enforce_cooldown=True,
            )
            previous_status = getattr(invite, "_audit_prev_status", None)
            previous_role_name = getattr(invite, "_audit_prev_role_name", None)
            record_audit_event(
                workspace=workspace,
                object_type="workspace_invite",
                object_id=invite.id,
                record_label=invite.email,
                action=(
                    AuditLog.ACTION_CREATE
                    if previous_status is None
                    else AuditLog.ACTION_UPDATE
                ),
                field_name="invite_status",
                previous_value=previous_status,
                new_value="Pending",
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
            )
            if previous_status is None or previous_role_name != target_role.name:
                record_audit_event(
                    workspace=workspace,
                    object_type="workspace_invite",
                    object_id=invite.id,
                    record_label=invite.email,
                    action=(
                        AuditLog.ACTION_CREATE
                        if previous_status is None
                        else AuditLog.ACTION_UPDATE
                    ),
                    field_name="role",
                    previous_value=previous_role_name,
                    new_value=target_role.name,
                    actor_kind=AuditLog.ACTOR_KIND_PERSON,
                    actor_user=user,
                )
    except EmailThrottledError as exc:
        return JsonResponse({"error": str(exc)}, status=429)
    except EmailDeliveryError:
        return JsonResponse(
            {"error": EMAIL_SERVICE_UNAVAILABLE_MESSAGE},
            status=503,
        )

    return JsonResponse(
        {
            "invite": serialize_invite(invite),
            "message": f"Invitation sent to {invite.email} ({target_role.name}).",
        },
        status=201,
    )


@require_http_methods(["POST"])
def workspace_invite_revoke_view(request, slug, invite_id: int):
    """Revoke a pending invite belonging to this workspace."""
    user, workspace, error_response = require_workspace_member(request, slug)
    if error_response is not None:
        return error_response

    if not user_can_manage_users_and_roles(user, workspace):
        return JsonResponse(
            {
                "error": (
                    "Only the workspace Owner or a role with users-and-roles "
                    "access can manage invites."
                )
            },
            status=403,
        )

    with transaction.atomic():
        invite = (
            WorkspaceInvite.objects.select_for_update(of=("self",))
            .select_related("invited_by", "role")
            .filter(id=invite_id, workspace=workspace)
            .first()
        )
        if invite is None:
            return JsonResponse({"error": "Invite not found."}, status=404)
        if invite.accepted_at is not None:
            return JsonResponse(
                {"error": "This invite has already been accepted and cannot be revoked."},
                status=400,
            )
        if invite.revoked_at is not None:
            return JsonResponse(
                {"error": "This invite has already been revoked."},
                status=400,
            )
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at", "updated_at"])
        record_audit_event(
            workspace=workspace,
            object_type="workspace_invite",
            object_id=invite.id,
            record_label=invite.email,
            action=AuditLog.ACTION_UPDATE,
            field_name="invite_status",
            previous_value="Pending",
            new_value="Revoked",
            actor_kind=AuditLog.ACTOR_KIND_PERSON,
            actor_user=user,
        )

    return JsonResponse({"invite": serialize_invite(invite)}, status=200)


def _inspect_invite_token(
    raw_token,
    *,
    for_update: bool = False,
    unknown_status: int = 404,
):
    if not isinstance(raw_token, str) or not raw_token.strip():
        return None, JsonResponse({"error": "Invite token is required."}, status=400)

    token_digest = hash_token(raw_token.strip())
    qs = WorkspaceInvite.objects.select_related("workspace", "invited_by", "role")
    if for_update:
        qs = qs.select_for_update(of=("self",))

    invite = qs.filter(token_hash=token_digest).first()
    if invite is None:
        return None, JsonResponse(
            {"error": "This invite link is invalid."}, status=unknown_status
        )
    if invite.accepted_at is not None:
        return None, JsonResponse(
            {"error": "This invite has already been accepted."}, status=400
        )
    if invite.revoked_at is not None:
        return None, JsonResponse(
            {"error": "This invite has been revoked."}, status=400
        )
    if invite.expires_at <= timezone.now():
        return None, JsonResponse(
            {"error": "This invite link has expired."}, status=400
        )

    user_qs = User.objects.filter(email__iexact=invite.email)
    if for_update:
        user_qs = user_qs.select_for_update()
    if user_qs.exists():
        return None, JsonResponse(
            {"error": SINGLE_WORKSPACE_ERROR_MESSAGE}, status=409
        )

    return invite, None


@require_GET
def invite_preview_view(request):
    """Validate an invite token for display without consuming it or checking location."""
    invite, error_response = _inspect_invite_token(
        request.GET.get("token"),
        for_update=False,
        unknown_status=404,
    )
    if error_response is not None:
        return error_response

    return JsonResponse(
        {
            "email": invite.email,
            "role": serialize_user_role_summary(invite.role, is_owner_fallback=False),
            "workspace": {
                "name": invite.workspace.name,
                "slug": invite.workspace.slug,
            },
            "invited_by_email": invite.invited_by.email if invite.invited_by else None,
            "expires_at": invite.expires_at.isoformat(),
        },
        status=200,
    )


@require_http_methods(["POST"])
def invite_accept_view(request):
    """Accept a workspace invite from any connection country without geo-checking."""
    payload, error_response = parse_json_object(request)
    if error_response is not None:
        return error_response

    raw_token = payload.get("token")
    if not isinstance(raw_token, str) or not raw_token.strip():
        return JsonResponse({"error": "Invite token is required."}, status=400)

    password = payload.get("password")
    password_error = validate_password_basics(password)
    if password_error is not None:
        return password_error

    resolved_timezone, tz_error = parse_timezone_field(
        payload.get("timezone"),
        required=False,
        default="Africa/Accra",
    )
    if tz_error is not None:
        return tz_error

    try:
        with transaction.atomic():
            invite, token_error = _inspect_invite_token(
                raw_token,
                for_update=True,
                unknown_status=400,
            )
            if token_error is not None:
                return token_error

            candidate_user = User(
                email=invite.email,
                workspace=invite.workspace,
                timezone=resolved_timezone,
            )
            password_error = validate_new_password(password, user=candidate_user)
            if password_error is not None:
                transaction.set_rollback(True)
                return password_error

            now = timezone.now()
            _owner_role, staff_role = ensure_workspace_default_roles(invite.workspace)
            assigned_role = staff_role
            if (
                invite.role is not None
                and invite.role.workspace_id == invite.workspace_id
                and not invite.role.is_owner
            ):
                assigned_role = invite.role

            user = User.objects.create_user(
                email=invite.email,
                workspace=invite.workspace,
                role=assigned_role,
                password=password,
                timezone=resolved_timezone,
                email_verified=True,
                email_verified_at=now,
            )
            invite.accepted_at = now
            invite.accepted_by = user
            invite.save(update_fields=["accepted_at", "accepted_by", "updated_at"])
            record_audit_event(
                workspace=invite.workspace,
                object_type="workspace_invite",
                object_id=invite.id,
                record_label=user.email,
                action=AuditLog.ACTION_UPDATE,
                field_name="member_joined",
                previous_value="Pending",
                new_value=assigned_role.name,
                actor_kind=AuditLog.ACTOR_KIND_PERSON,
                actor_user=user,
            )
    except IntegrityError:
        return JsonResponse(
            {"error": SINGLE_WORKSPACE_ERROR_MESSAGE},
            status=409,
        )

    if request.user.is_authenticated:
        logout(request)

    login(request, user)
    return JsonResponse(
        serialize_workspace_and_user(invite.workspace, user),
        status=200,
    )
