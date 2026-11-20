from django.urls import path

from .views.account_views import (
    account_password_change_view,
    account_password_set_view,
    account_profile_view,
    auth_methods_view,
)
from .views.auth_views import (
    auth_session_view,
    login_view,
    logout_view,
    password_reset_confirm_view,
    password_reset_request_view,
    resend_verification_view,
    signup_view,
    verify_email_view,
)
from .views.crm_views import (
    workspace_crm_options_view,
    workspace_dashboard_view,
    workspace_deals_board_view,
    workspace_lead_conversion_preview_view,
    workspace_lead_convert_view,
    workspace_record_activities_view,
    workspace_record_audit_view,
    workspace_record_collection_view,
    workspace_record_detail_view,
)
from .views.search_views import workspace_search_view
from .views.pipeline_views import (
    workspace_pipeline_stage_detail_view,
    workspace_pipeline_stages_reorder_view,
    workspace_pipeline_stages_view,
    workspace_pipelines_view,
)
from .views.invite_views import (
    invite_accept_view,
    invite_preview_view,
    workspace_invite_create_view,
    workspace_invite_revoke_view,
    workspace_people_view,
)
from .views.oauth_views import (
    auth_provider_unlink_view,
    auth_providers_view,
    google_oauth_callback_view,
    google_oauth_start_view,
    microsoft_oauth_callback_view,
    microsoft_oauth_start_view,
)
from .views.passkey_views import (
    passkey_delete_view,
    passkey_login_options_view,
    passkey_login_verify_view,
    passkey_register_options_view,
    passkey_register_verify_view,
)
from .views.public_views import (
    business_type_catalog_view,
    csrf_token_view,
    hello_world,
)
from .views.workspace_access_views import (
    workspace_member_deactivate_view,
    workspace_member_delete_view,
    workspace_member_reactivate_view,
    workspace_role_detail_view,
    workspace_roles_view,
)
from .views.workspace_views import (
    workspace_audit_view,
    workspace_detail_view,
    workspace_packs_view,
    workspace_table_detail_view,
)

urlpatterns = [
    path('hello/', hello_world, name='hello_world'),
    path('catalog/business-types/', business_type_catalog_view, name='business_type_catalog'),
    path('csrf/', csrf_token_view, name='csrf_token'),
    path('signup/', signup_view, name='signup'),
    path('verify-email/', verify_email_view, name='verify_email'),
    path('resend-verification/', resend_verification_view, name='resend_verification'),
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path(
        'password-reset/request/',
        password_reset_request_view,
        name='password_reset_request',
    ),
    path(
        'password-reset/confirm/',
        password_reset_confirm_view,
        name='password_reset_confirm',
    ),
    path('auth/providers/', auth_providers_view, name='auth_providers'),
    path(
        'auth/providers/<str:provider>/',
        auth_provider_unlink_view,
        name='auth_provider_unlink',
    ),
    path('auth/session/', auth_session_view, name='auth_session'),
    path('auth/methods/', auth_methods_view, name='auth_methods'),
    path('account/profile/', account_profile_view, name='account_profile'),
    path(
        'account/password/change/',
        account_password_change_view,
        name='account_password_change',
    ),
    path(
        'account/password/set/',
        account_password_set_view,
        name='account_password_set',
    ),
    path('invites/preview/', invite_preview_view, name='invite_preview'),
    path('invites/accept/', invite_accept_view, name='invite_accept'),
    path('auth/google/start/', google_oauth_start_view, name='google_oauth_start'),
    path('auth/google/callback/', google_oauth_callback_view, name='google_oauth_callback'),
    path('auth/microsoft/start/', microsoft_oauth_start_view, name='microsoft_oauth_start'),
    path('auth/microsoft/callback/', microsoft_oauth_callback_view, name='microsoft_oauth_callback'),
    path(
        'auth/passkey/register/options/',
        passkey_register_options_view,
        name='passkey_register_options',
    ),
    path(
        'auth/passkey/register/verify/',
        passkey_register_verify_view,
        name='passkey_register_verify',
    ),
    path(
        'auth/passkey/<int:passkey_id>/',
        passkey_delete_view,
        name='passkey_delete',
    ),
    path(
        'auth/passkey/login/options/',
        passkey_login_options_view,
        name='passkey_login_options',
    ),
    path(
        'auth/passkey/login/verify/',
        passkey_login_verify_view,
        name='passkey_login_verify',
    ),
    path('workspaces/<slug:slug>/', workspace_detail_view, name='workspace_detail'),
    path('workspaces/<slug:slug>/people/', workspace_people_view, name='workspace_people'),
    path('workspaces/<slug:slug>/invites/', workspace_invite_create_view, name='workspace_invite_create'),
    path(
        'workspaces/<slug:slug>/invites/<int:invite_id>/revoke/',
        workspace_invite_revoke_view,
        name='workspace_invite_revoke',
    ),
    path(
        'workspaces/<slug:slug>/members/<int:member_id>/',
        workspace_member_delete_view,
        name='workspace_member_delete',
    ),
    path(
        'workspaces/<slug:slug>/members/<int:member_id>/deactivate/',
        workspace_member_deactivate_view,
        name='workspace_member_deactivate',
    ),
    path(
        'workspaces/<slug:slug>/members/<int:member_id>/reactivate/',
        workspace_member_reactivate_view,
        name='workspace_member_reactivate',
    ),
    path('workspaces/<slug:slug>/roles/', workspace_roles_view, name='workspace_roles'),
    path(
        'workspaces/<slug:slug>/roles/<int:role_id>/',
        workspace_role_detail_view,
        name='workspace_role_detail',
    ),
    path('workspaces/<slug:slug>/dashboard/', workspace_dashboard_view, name='workspace_dashboard'),
    path('workspaces/<slug:slug>/search/', workspace_search_view, name='workspace_search'),
    path('workspaces/<slug:slug>/crm/options/', workspace_crm_options_view, name='workspace_crm_options'),
    path('workspaces/<slug:slug>/deals/board/', workspace_deals_board_view, name='workspace_deals_board'),
    path('workspaces/<slug:slug>/pipelines/', workspace_pipelines_view, name='workspace_pipelines'),
    path(
        'workspaces/<slug:slug>/pipelines/stages/',
        workspace_pipeline_stages_view,
        name='workspace_pipeline_stages',
    ),
    path(
        'workspaces/<slug:slug>/pipelines/stages/reorder/',
        workspace_pipeline_stages_reorder_view,
        name='workspace_pipeline_stages_reorder',
    ),
    path(
        'workspaces/<slug:slug>/pipelines/stages/<int:stage_id>/',
        workspace_pipeline_stage_detail_view,
        name='workspace_pipeline_stage_detail',
    ),
    path(
        'workspaces/<slug:slug>/records/<str:object_key>/',
        workspace_record_collection_view,
        name='workspace_record_collection',
    ),
    path(
        'workspaces/<slug:slug>/records/<str:object_key>/<int:record_id>/',
        workspace_record_detail_view,
        name='workspace_record_detail',
    ),
    path(
        'workspaces/<slug:slug>/records/<str:object_key>/<int:record_id>/activities/',
        workspace_record_activities_view,
        name='workspace_record_activities',
    ),
    path(
        'workspaces/<slug:slug>/records/<str:object_key>/<int:record_id>/audit/',
        workspace_record_audit_view,
        name='workspace_record_audit',
    ),
    path(
        'workspaces/<slug:slug>/records/lead/<int:record_id>/conversion-preview/',
        workspace_lead_conversion_preview_view,
        name='workspace_lead_conversion_preview',
    ),
    path(
        'workspaces/<slug:slug>/records/leads/<int:record_id>/conversion-preview/',
        workspace_lead_conversion_preview_view,
        name='workspace_leads_conversion_preview',
    ),
    path(
        'workspaces/<slug:slug>/records/lead/<int:record_id>/convert/',
        workspace_lead_convert_view,
        name='workspace_lead_convert',
    ),
    path(
        'workspaces/<slug:slug>/records/leads/<int:record_id>/convert/',
        workspace_lead_convert_view,
        name='workspace_leads_convert',
    ),
    path('workspaces/<slug:slug>/packs/', workspace_packs_view, name='workspace_packs'),
    path(
        'workspaces/<slug:slug>/tables/<str:object_key>/',
        workspace_table_detail_view,
        name='workspace_table_detail',
    ),
    path('workspaces/<slug:slug>/audit/', workspace_audit_view, name='workspace_audit'),
]
