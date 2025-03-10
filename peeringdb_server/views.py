"""
View definitions:

- Login
- Logout
- Advanced search
- User Profile
- OAuth Profile
- Landing page
- Search results
- Entity views (network, facility, internet exchange and organization)
- Sponsorships
- User Registration
"""
import datetime
import json
import os
import re
import uuid

import oauth2_provider.views as oauth2_views
import oauth2_provider.views.application as oauth2_application_views
import requests
from allauth.account.models import EmailAddress
from django.conf import settings as dj_settings
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.forms.models import modelform_factory
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.template import loader
from django.urls import Resolver404, resolve, reverse
from django.utils import translation
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext_lazy as _
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django_grainy.util import Permissions
from django_otp.plugins.otp_email.models import EmailDevice
from django_security_keys.ext.two_factor.views import (  # noqa
    DisableView as TwoFactorDisableView,
)
from django_security_keys.ext.two_factor.views import LoginView as TwoFactorLoginView
from grainy.const import PERM_CREATE, PERM_CRUD, PERM_DELETE, PERM_UPDATE
from oauth2_provider.decorators import protected_resource
from oauth2_provider.models import get_application_model
from oauth2_provider.oauth2_backends import get_oauthlib_core
from ratelimit.decorators import ratelimit

from peeringdb_server import settings
from peeringdb_server.api_key_views import load_all_key_permissions
from peeringdb_server.data_views import BOOL_CHOICE, BOOL_CHOICE_WITH_OPT_OUT
from peeringdb_server.deskpro import ticket_queue_rdap_error
from peeringdb_server.forms import (
    AffiliateToOrgForm,
    OrganizationLogoUploadForm,
    PasswordChangeForm,
    PasswordResetForm,
    UserCreationForm,
    UserLocaleForm,
    UsernameRetrieveForm,
)
from peeringdb_server.inet import (
    RdapException,
    RdapInvalidRange,
    asn_is_bogon,
    rdap_pretty_error_message,
)
from peeringdb_server.mail import mail_username_retrieve
from peeringdb_server.models import (
    PARTNERSHIP_LEVELS,
    REFTAG_MAP,
    UTC,
    DataChangeWatchedObject,
    Facility,
    InternetExchange,
    InternetExchangeFacility,
    IXFMemberData,
    Network,
    NetworkContact,
    NetworkFacility,
    NetworkIXLan,
    Organization,
    Partnership,
    Sponsorship,
    User,
    UserOrgAffiliationRequest,
    UserPasswordReset,
)
from peeringdb_server.org_admin_views import load_all_user_permissions
from peeringdb_server.permissions import APIPermissionsApplicator, check_permissions
from peeringdb_server.search import search
from peeringdb_server.serializers import (
    FacilitySerializer,
    InternetExchangeSerializer,
    NetworkSerializer,
    OrganizationSerializer,
)
from peeringdb_server.stats import get_fac_stats, get_ix_stats
from peeringdb_server.stats import stats as global_stats

RATELIMITS = dj_settings.RATELIMITS


BASE_ENV = {
    "RECAPTCHA_PUBLIC_KEY": dj_settings.RECAPTCHA_PUBLIC_KEY,
    "OAUTH_ENABLED": dj_settings.OAUTH_ENABLED,
    "PEERINGDB_VERSION": settings.PEERINGDB_VERSION,
    "TUTORIAL_MODE": settings.TUTORIAL_MODE,
    "RELEASE_ENV": settings.RELEASE_ENV,
    "SHOW_AUTO_PROD_SYNC_WARNING": settings.SHOW_AUTO_PROD_SYNC_WARNING,
    "GOOGLE_ANALYTICS_ID": dj_settings.GOOGLE_ANALYTICS_ID,
}


def field_help(model, field):
    """
    Helper function return help_text of a model
    field.
    """
    return model._meta.get_field(field).help_text


def is_oauth_authorize(url):
    try:
        match = resolve(url)
        return match.url_name == "authorize"
    except Resolver404:
        return False


def export_permissions(user, entity):
    """
    Return dict of permission bools for the specified user and entity
    to be used in template context.
    """

    if entity.status == "deleted":
        return {}

    perms = {
        "can_write": check_permissions(user, entity, PERM_UPDATE),
        "can_create": check_permissions(user, entity, PERM_CREATE),
        "can_delete": check_permissions(user, entity, PERM_DELETE),
    }

    if entity.status == "pending":
        perms["can_create"] = False
        perms["can_delete"] = False

    if perms["can_write"] or perms["can_create"] or perms["can_delete"]:
        perms["can_edit"] = True

    if hasattr(entity, "grainy_namespace_manage"):
        perms["can_manage"] = check_permissions(
            user, entity.grainy_namespace_manage, PERM_CRUD
        )
    else:
        perms["can_manage"] = False

    return perms


class DoNotRender:
    """
    Instance of this class is sent when a component attribute does not exist,
    this can then be type checked in the templates to remove non existant attribute
    rows while still allowing attributes with nonetype values to be rendered.
    """

    @classmethod
    def permissioned(cls, value, user, namespace, explicit=False):
        """
        Check if the user has permissions to the supplied namespace
        returns a DoNotRender instance if not, otherwise returns
        the supplied value.
        """

        b = check_permissions(user, namespace.lower(), 0x01, explicit=explicit)
        if not b:
            return cls()
        return value

    def all(self):
        return []


def beta_sync_dt():
    """
    Return the next date for a beta sync.

    This is currently hard coded to return 00:00Z for the
    next Sunday.
    """
    dt = datetime.datetime.now() + datetime.timedelta(1)

    while dt.weekday() != 6:
        dt += datetime.timedelta(1)

    return dt.replace(hour=0, minute=0, second=0)


def update_env_beta_sync_dt(env):
    if settings.RELEASE_ENV == "beta":
        env.update(beta_sync_dt=beta_sync_dt())


def make_env(**data):
    env = {}
    env.update(**BASE_ENV)
    env.update(**{"global_stats": global_stats()})
    env.update(**data)
    update_env_beta_sync_dt(env)

    return env


def view_http_error_404(request):
    template = loader.get_template("site/error_404.html")
    return HttpResponseNotFound(template.render(make_env(), request))


def view_http_error_403(request):
    template = loader.get_template("site/error_403.html")
    return HttpResponseForbidden(template.render(make_env(), request))


def view_http_error_csrf(request, reason):
    return JsonResponse({"non_field_errors": [reason]}, status=403)


def view_http_error_invalid(request, reason):
    return JsonResponse({"non_field_errors": [reason]}, status=400)


def view_maintenance(request):
    template = loader.get_template("site/maintenance.html")
    return HttpResponse(template.render({}, request), status=503)


@login_required
@transaction.atomic
@ratelimit(key="ip", rate=RATELIMITS["view_request_ownership_GET"], method="GET")
@ratelimit(key="ip", rate=RATELIMITS["view_request_ownership_POST"], method="POST")
def view_request_ownership(request):
    """
    Render the form that allows users to request ownership
    to an unclaimed organization.
    """

    was_limited = getattr(request, "limited", False)

    if request.method in ["GET", "HEAD"]:

        # check if reuqest was blocked by rate limiting
        if was_limited:
            return view_index(
                request,
                errors=[_("Please wait a bit before requesting ownership again.")],
            )

        org_id = request.GET.get("id")
        try:
            org = Organization.objects.get(id=org_id)
        except Organization.DoesNotExist:
            return view_index(request, errors=[_("Invalid organization")])

        if org.owned:
            return view_index(
                request,
                errors=[
                    _("Organization '%(org_name)s' is already under ownership")
                    % {"org_name": org.name}
                ],
            )

        template = loader.get_template("site/request-ownership.html")
        return HttpResponse(template.render(make_env(org=org), request))

    elif request.method == "POST":

        org_id = request.POST.get("id")

        # check if reuqest was blocked by rate limiting
        if was_limited:
            return JsonResponse(
                {
                    "non_field_errors": [
                        _("Please wait a bit before requesting ownership again.")
                    ]
                },
                status=400,
            )

        try:
            org = Organization.objects.get(id=org_id)
        except Organization.DoesNotExist:
            return JsonResponse(
                {"non_field_errors": [_("Organization does not exist")]}, status=400
            )

        if org.owned:
            return JsonResponse(
                {
                    "non_field_errors": [
                        _("Organization '%(org_name)s' is already under ownership")
                        % {"org_name": org.name}
                    ]
                },
                status=400,
            )

        if UserOrgAffiliationRequest.objects.filter(
            user=request.user, org=org, status="pending"
        ).exists():
            return JsonResponse(
                {
                    "non_field_errors": [
                        _(
                            "You already have an ownership request pending for this organization"
                        )
                    ]
                },
                status=400,
            )

        if not request.user.affiliation_requests_available:
            return view_http_error_invalid(
                request,
                _(
                    "You have too many affiliation requests pending, "
                    "please wait for them to be resolved before opening more."
                ),
            )

        request.user.flush_affiliation_requests()

        uoar = UserOrgAffiliationRequest.objects.create(
            user=request.user, org=org, status="pending"
        )
        return JsonResponse({"status": "ok", "ownership_status": uoar.status})


@csrf_protect
@transaction.atomic
@require_http_methods(["POST"])
def cancel_affiliation_request(request, uoar_id):
    """
    Cancel a user's affiliation request.
    """

    # make sure user org affiliation request specified actually
    # belongs to requesting user

    try:
        affiliation_request = request.user.pending_affiliation_requests.get(id=uoar_id)
    except UserOrgAffiliationRequest.DoesNotExist:
        return view_http_error_404(request)

    affiliation_request.cancel()

    return redirect(reverse("user-profile"))


@csrf_protect
@ensure_csrf_cookie
@login_required
@transaction.atomic
@ratelimit(key="ip", method="POST", rate=RATELIMITS["view_affiliate_to_org_POST"])
def view_affiliate_to_org(request):
    """
    Allow the user to request affiliation with an organization through
    an ASN they provide.
    """

    if request.method == "POST":

        # check if request was blocked by rate limiting
        was_limited = getattr(request, "limited", False)
        if was_limited:
            return JsonResponse(
                {
                    "non_field_errors": [
                        _("Please wait a bit before requesting affiliation again.")
                    ]
                },
                status=400,
            )

        if not request.user.affiliation_requests_available:
            return view_http_error_invalid(
                request,
                _(
                    "You have too many affiliation requests pending, "
                    "please wait for them to be resolved before opening more."
                ),
            )

        form = AffiliateToOrgForm(request.POST)
        if not form.is_valid():
            return JsonResponse(form.errors, status=400)

        if (
            not form.cleaned_data.get("org")
            and not form.cleaned_data.get("asn")
            and not form.cleaned_data.get("org_name")
        ):
            return JsonResponse(
                {
                    "asn": _("Either ASN or Organization required"),
                    "org": _("Either ASN or Organization required"),
                },
                status=400,
            )

        asn = form.cleaned_data.get("asn")
        org_id = form.cleaned_data.get("org")
        org_name = form.cleaned_data.get("org_name")

        # Issue 931: Limit the number of requests
        # for affiliation to an ASN/org to 1

        # Need to match ASN to org id
        # if network exists
        if asn != 0 and Network.objects.filter(asn=asn).exists():
            network = Network.objects.get(asn=asn)
            org_id = network.org.id

        if org_id:
            if Organization.objects.get(id=org_id).status == "deleted":
                return JsonResponse(
                    {
                        "non_field_errors": [
                            _(
                                "Unable to affiliate as this organization has been deleted. Please reach out to PeeringDB support if you wish to resolve this."
                            )
                        ]
                    },
                    status=400,
                )

        already_requested_affil_response = JsonResponse(
            {
                "non_field_errors": [
                    _("You already requested affiliation to this ASN/org")
                ]
            },
            status=400,
        )

        pending_affil_reqs = request.user.pending_affiliation_requests
        if org_id and pending_affil_reqs.filter(org_id=org_id).exists():
            return already_requested_affil_response
        elif asn and pending_affil_reqs.filter(asn=asn).exists():
            return already_requested_affil_response
        elif org_name and pending_affil_reqs.filter(org_name__iexact=org_name).exists():
            return already_requested_affil_response

        request.user.flush_affiliation_requests()

        try:
            # Issue 995: Block registering private ASN ranges
            # Check if ASN is in private/reserved range
            # Block submission if an org and private ASN is set
            if asn_is_bogon(asn) and not settings.TUTORIAL_MODE:
                raise RdapInvalidRange()

            UserOrgAffiliationRequest.objects.get_or_create(
                user=request.user,
                asn=form.cleaned_data.get("asn"),
                org_id=form.cleaned_data.get("org") or None,
                org_name=form.cleaned_data.get("org_name") or None,
                status="pending",
            )

        except RdapInvalidRange as exc:

            return JsonResponse({"asn": rdap_pretty_error_message(exc)}, status=400)

        except RdapException as exc:

            ticket_queue_rdap_error(request, asn, exc)

            return JsonResponse({"asn": rdap_pretty_error_message(exc)}, status=400)

        except MultipleObjectsReturned:
            pass

        return JsonResponse({"status": "ok"})

    return view_verify(request)


@csrf_protect
@ensure_csrf_cookie
@login_required
@transaction.atomic
@ratelimit(key="ip", rate=RATELIMITS["resend_confirmation_mail"])
def resend_confirmation_mail(request):
    was_limited = getattr(request, "limited", False)
    if was_limited:
        return view_index(
            request,
            errors=[
                _(
                    "Please wait a bit before trying to resend the confirmation email again"
                )
            ],
        )

    request.user.send_email_confirmation(
        request=request, email=request.POST.get("email")
    )
    return view_index(request, errors=[_("We have resent your confirmation email")])


@csrf_protect
@ensure_csrf_cookie
def view_profile(request):
    return view_verify(request)


@csrf_protect
@ensure_csrf_cookie
@login_required
@transaction.atomic
def view_set_user_locale(request):

    if request.method in ["GET", "HEAD"]:
        return view_verify(request)
    elif request.method == "POST":

        form = UserLocaleForm(request.POST)
        if not form.is_valid():
            return JsonResponse(form.errors, status=400)

        loc = form.cleaned_data.get("locale")
        if loc in [lang[0] for lang in dj_settings.LANGUAGES]:
            request.user.set_locale(loc)
        else:
            return JsonResponse(
                {"error": _("Malformed Language Preference")}, status=400
            )

        translation.activate(loc)
        response = JsonResponse({"status": "ok"})
        response.set_cookie(dj_settings.LANGUAGE_COOKIE_NAME, loc)

        return response


# OAuth application management overrides


class ApplicationOwnerMixin:

    """
    OAuth mixin it that filters application queryset for ownership
    considering either the owning user or the owning organization.

    For organizations any user in the administrator group for the organization
    may manage the oauth application
    """

    def get_queryset(self):

        org_ids = [org.id for org in self.request.user.admin_organizations]

        return get_application_model().objects.filter(
            Q(user=self.request.user) | Q(org_id__in=org_ids)
        )


class ApplicationFormMixin:

    """
    Used for oauth application update and registration process

    Will add an `org` field to the form and make sure it is filtered to only contain
    organizations the requesting user has management permissions to
    """

    def get_form_class(self):
        """
        Returns the form class for the application model
        """
        return modelform_factory(
            get_application_model(),
            fields=(
                "org",
                "name",
                "client_id",
                "client_secret",
                "client_type",
                "authorization_grant_type",
                "redirect_uris",
                "algorithm",
            ),
            labels={"org": _("Organization")},
            help_texts={"org": _("Register on behalf of one of your organizations")},
        )

    def get_form(self):

        form = super().get_form()

        # filter organization choices to only contain organizations manageable
        # by the requesting user

        org_ids = [org.id for org in self.request.user.admin_organizations]
        form.fields["org"].queryset = Organization.objects.filter(id__in=org_ids)

        return form


class ApplicationRegistration(
    ApplicationFormMixin, oauth2_application_views.ApplicationRegistration
):
    def form_valid(self, form):
        r = super().form_valid(form)
        if form.instance.org:
            form.instance.user = None
            form.instance.save()
        return r

    def get_form(self):
        form = super().get_form()

        # if url parameter `org` is provided attempt to use
        # it to preselect the choice in the `org` field dropdown

        org_id = self.request.GET.get("org")
        if org_id:
            form.fields["org"].initial = org_id
        return form


oauth2_views.ApplicationRegistration = ApplicationRegistration


class ApplicationDetail(ApplicationOwnerMixin, oauth2_views.ApplicationDetail):
    @never_cache
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


oauth2_views.ApplicationDetail = ApplicationDetail


class ApplicationList(ApplicationOwnerMixin, oauth2_views.ApplicationList):
    pass


oauth2_views.ApplicationList = ApplicationList


class ApplicationDelete(ApplicationOwnerMixin, oauth2_views.ApplicationDelete):
    pass


oauth2_views.ApplicationDelete = ApplicationDelete


class ApplicationUpdate(
    ApplicationOwnerMixin, ApplicationFormMixin, oauth2_views.ApplicationUpdate
):
    def form_valid(self, form):
        if not form.instance.org and not form.instance.user:
            form.instance.user = self.request.user
        return super().form_valid(form)


oauth2_views.ApplicationUpdate = ApplicationUpdate


# OAuth profile


@protected_resource(scopes=["profile"])
def view_profile_v1(request):
    oauth = get_oauthlib_core()
    scope_email, _request = oauth.verify_request(request, scopes=["email"])
    scope_networks, _request = oauth.verify_request(request, scopes=["networks"])

    json_params = {}
    if "pretty" in request.GET:
        json_params["indent"] = 2

    user = request.user
    data = dict(
        id=request.user.id,
        given_name=request.user.first_name,
        family_name=request.user.last_name,
        name=request.user.full_name,
        verified_user=user.is_verified_user,
    )

    # only add email fields if email scope is present
    if scope_email:
        data.update(dict(email=request.user.email, verified_email=user.email_confirmed))

    # only add ddnetworks if networks scope is present
    if scope_networks:
        networks = []
        perms = Permissions(user)
        for net in user.networks:
            crud = perms.get(net.grainy_namespace)
            networks.append(dict(id=net.id, name=net.name, asn=net.asn, perms=crud))

        data["networks"] = networks

    return JsonResponse(data, json_dumps_params=json_params)


@csrf_protect
@ensure_csrf_cookie
@login_required
@require_http_methods(["GET"])
def view_verify(request):
    template = loader.get_template("site/verify.html")
    env = BASE_ENV.copy()
    env.update(
        {"affiliations": request.user.organizations, "global_stats": global_stats()}
    )
    return HttpResponse(template.render(env, request))


@csrf_protect
@ensure_csrf_cookie
@login_required
@transaction.atomic
@require_http_methods(["POST"])
@ratelimit(key="ip", rate=RATELIMITS["view_verify_POST"], method="POST")
def profile_add_email(request):

    """
    Allows a user to add an additional email address
    """

    password = request.POST.get("password")
    email = request.POST.get("email")
    make_primary = (
        request.POST.get("primary") == "true" or request.POST.get("primary") is True
    )

    was_limited = getattr(request, "limited", False)

    # handle rate limiting

    if was_limited:
        return JsonResponse(
            {
                "non_field_errors": [
                    _("Please wait a bit before requesting another email change")
                ]
            },
            status=400,
        )

    # password authentication is required

    if not authenticate(username=request.user.username, password=password):
        return JsonResponse(
            {
                "status": "auth",
                "non_field_errors": [_("Invalid password. Please try again.")],
            },
            status=401,
        )

    # address already exists, bail with an error response

    if EmailAddress.objects.filter(email=email).exists():
        return JsonResponse(
            {"non_field_errors": [_("E-mail already exists in our system")]}, status=400
        )

    # user has reached max limit of email addresses, bail with error response

    if request.user.emailaddress_set.count() >= dj_settings.USER_MAX_EMAIL_ADDRESSES:
        return JsonResponse(
            {
                "non_field_errors": [
                    _("You may have a maximum of {} email addresses").format(
                        dj_settings.USER_MAX_EMAIL_ADDRESSES
                    )
                ]
            },
            status=400,
        )

    # add new email address

    email_obj = EmailAddress.objects.create(email=email, user=request.user)

    # make new email address primary email addess

    if make_primary:
        email_obj.set_as_primary()
        request.user.email = email
        request.user.clean()
        request.user.save()

    # create log entry for django-admin

    LogEntry.objects.log_action(
        request.user.id,
        ContentType.objects.get_for_model(User).id,
        request.user.id,
        f"{email_obj}",
        CHANGE,
        change_message=f"User added email address {email}",
    )

    # send email confirmation process

    request.user.send_email_confirmation(request=request, email=email)

    # send notification that an email has been added to the user's account

    request.user.notify_email_added(email)

    return JsonResponse({"status": "ok", "email": email, "primary": make_primary})


@csrf_protect
@ensure_csrf_cookie
@login_required
@transaction.atomic
@require_http_methods(["POST"])
def profile_delete_email(request):

    """
    Allows a user to remove one of their emails
    """

    email = request.POST.get("email")

    # if the specified email does not exist, just return

    try:
        email = EmailAddress.objects.get(user=request.user, email=email)
    except EmailAddress.DoesNotExist:
        return JsonResponse({"status": "ok"})

    # primary email address cannot be removed

    if email.primary:
        return JsonResponse(
            {"non_field_errors": [_("Cannot remove primary email")]}, status=400
        )

    # remove email

    email.delete()

    # create log entry for django-admin

    LogEntry.objects.log_action(
        request.user.id,
        ContentType.objects.get_for_model(User).id,
        request.user.id,
        f"{email}",
        CHANGE,
        change_message=f"User removed email address {email.email}",
    )

    # let the user know that their email was removed

    request.user.notify_email_removed(email.email)

    return JsonResponse({"status": "ok"})


@csrf_protect
@ensure_csrf_cookie
@login_required
@transaction.atomic
@require_http_methods(["POST"])
def profile_set_primary_email(request):

    """
    Allows a user to set a different email address as their primary
    contact point for peeringdb
    """

    email = request.POST.get("email")

    # check that email exists

    try:
        email = EmailAddress.objects.get(user=request.user, email=email)
    except EmailAddress.DoesNotExist:
        return JsonResponse(
            {"non_field_errors": [_("Email address not found")]}, status=400
        )

    # set as primary

    email.set_as_primary()

    return JsonResponse({"status": "ok"})


@csrf_protect
@ensure_csrf_cookie
@login_required
@transaction.atomic
def view_password_change(request):

    if request.method in ["GET", "HEAD"]:
        return view_verify(request)
    elif request.method == "POST":

        password_c = request.POST.get("password_c")

        if not request.user.has_oauth:
            if not authenticate(username=request.user.username, password=password_c):
                return JsonResponse(
                    {"status": "auth", "password_c": _("Wrong password")}, status=400
                )
        else:
            return JsonResponse({"status": "auth"}, status=401)

        form = PasswordChangeForm(request.POST)
        if not form.is_valid():
            return JsonResponse(form.errors, status=400)

        request.user.set_password(form.cleaned_data.get("password"))
        request.user.save()

        return JsonResponse({"status": "ok"})


@ensure_csrf_cookie
@require_http_methods(["GET"])
def view_username_retrieve(request):
    """
    Username retrieval view.
    """
    env = BASE_ENV.copy()
    env.update({"global_stats": global_stats()})
    return render(request, "site/username-retrieve.html", env)


@csrf_protect
@ensure_csrf_cookie
@require_http_methods(["POST"])
@transaction.atomic
@ratelimit(key="ip", rate=RATELIMITS["view_username_retrieve_initiate"])
def view_username_retrieve_initiate(request):
    """
    Username retrieval initiate view.
    """

    was_limited = getattr(request, "limited", False)
    if was_limited:
        return JsonResponse(
            {
                "non_field_errors": [
                    _("Please wait a bit before requesting your usernames again.")
                ]
            },
            status=400,
        )

    # clean form and get email address
    form = UsernameRetrieveForm(request.POST)
    if not form.is_valid():
        return JsonResponse(form.errors, status=400)

    email = form.cleaned_data.get("email")

    # generate secret and store in user's django sessions
    secret = str(uuid.uuid4())
    request.session["username_retrieve_secret"] = secret
    request.session["username_retrieve_email"] = email

    # send email
    if User.objects.filter(email=email).exists():
        mail_username_retrieve(email, secret)

    return JsonResponse({"status": "ok"})


@ensure_csrf_cookie
@require_http_methods(["GET"])
def view_username_retrieve_complete(request):
    """
    Username retrieval completion view.

    Show the list of usernames associated to an email if
    the correct secret is provided.
    """

    secret = request.GET.get("secret")
    secret_expected = request.session.get("username_retrieve_secret")
    email = request.session.get("username_retrieve_email")
    env = BASE_ENV.copy()
    env.update(
        {
            "secret": secret,
            "secret_expected": secret_expected,
            "users": User.objects.filter(email=email),
            "email": email,
        }
    )

    if secret_expected and constant_time_compare(secret, secret_expected):
        # invalidate the username retrieve session
        del request.session["username_retrieve_email"]
        del request.session["username_retrieve_secret"]
        request.session.modified = True

    return render(request, "site/username-retrieve-complete.html", env)


@csrf_protect
@ensure_csrf_cookie
@transaction.atomic
def view_password_reset(request):
    """
    Password reset initiation view.
    """

    if request.method in ["GET", "HEAD"]:
        env = BASE_ENV.copy()
        env.update({"global_stats": global_stats()})

        env["token"] = token = request.GET.get("token")
        env["target"] = target = request.GET.get("target")

        if token and target:
            pr = UserPasswordReset.objects.filter(user_id=target).first()
            env["pr"] = pr

            if pr and pr.match(token) and pr.is_valid():
                env["username"] = pr.user.username
                env["token_valid"] = True

        template = loader.get_template("site/password-reset.html")

        return HttpResponse(template.render(env, request))

    elif request.method == "POST":

        token = request.POST.get("token")
        target = request.POST.get("target")
        if token and target:
            form = PasswordChangeForm(request.POST)
            if not form.is_valid():
                return JsonResponse(form.errors, status=400)

            user = User.objects.filter(id=target).first()

            err_invalid_token_msg = _("Invalid Security Token")
            err_expired_msg = ('{} <a href="/reset-password">{}</a>').format(
                _("Password Reset Process has expired, please"), _("initiate again")
            )

            if user:
                try:
                    if not user.password_reset.match(token):
                        return JsonResponse(
                            {"non_field_errors": [err_invalid_token_msg]}, status=400
                        )

                    if not user.password_reset.is_valid():
                        return JsonResponse(
                            {"non_field_errors": [err_expired_msg]}, status=400
                        )

                except UserPasswordReset.DoesNotExist:
                    return JsonResponse(
                        {"non_field_errors": [err_expired_msg]}, status=400
                    )

                user.password_reset_complete(token, form.cleaned_data.get("password"))

            else:
                return JsonResponse({"non_field_errors": [err_expired_msg]}, status=400)

        else:
            form = PasswordResetForm(request.POST)

            if not form.is_valid():
                return JsonResponse(form.errors, status=400)

            user = User.objects.filter(email=form.cleaned_data["email"]).first()
            if user:
                user.password_reset_initiate()
        return JsonResponse({"status": "ok"})


@csrf_protect
@ensure_csrf_cookie
@transaction.atomic
def view_registration(request):
    """
    User registration page view.
    """
    if request.user.is_authenticated:
        return view_index(
            request,
            errors=[
                _(
                    "Please log out of your current session before trying to register. Notice, multiple accounts are no longer needed."
                )
            ],
        )

    if request.method in ["GET", "HEAD"]:
        template = loader.get_template("site/register.html")
        env = BASE_ENV.copy()
        env.update(
            {"global_stats": global_stats(), "register_form": UserCreationForm()}
        )
        update_env_beta_sync_dt(env)
        return HttpResponse(template.render(env, request))

    elif request.method == "POST":
        form = UserCreationForm(request.POST)
        form.request = request

        if not form.is_valid():
            errors = form.errors
            errors["non_field_errors"] = errors.get("__all__", [])
            return JsonResponse(errors, status=400)

        email = form.cleaned_data["email"]
        if EmailAddress.objects.filter(email=email).count() > 0:
            return JsonResponse(
                {"email": _("This email address has already been used")}, status=400
            )

        # require min password length
        # FIXME: impl password strength validation
        if len(form.cleaned_data["password1"]) < 10:
            return JsonResponse(
                {"password1": _("Needs to be at least 10 characters long")}, status=400
            )
        # filter out invalid username characters
        if form.cleaned_data["username"].startswith("apikey"):
            return JsonResponse(
                {"username": _("Username cannot start with 'apikey'")}, status=400
            )
        # create the user
        user = form.save()

        user.set_unverified()

        # log the user in
        login(
            request,
            authenticate(
                username=request.POST["username"], password=request.POST["password1"]
            ),
        )

        user.send_email_confirmation(signup=True, request=request)

        form.delete_captcha()

        return JsonResponse({"status": "ok"})


@csrf_protect
@ensure_csrf_cookie
@login_required
def view_close_account(request):
    """
    Set user's account inactive, delete email addresses and API keys and logout the session.
    """

    password = request.POST.get("password")

    # If user is authenticated, check password
    if request.user.is_authenticated:
        if request.user.check_password(password):

            request.user.close_account()

            # Logout the user
            logout(request)

            return JsonResponse({"status": "ok"})
        else:
            # Password didn't match
            return JsonResponse({"password": _("Incorrect password")}, status=400)


@ensure_csrf_cookie
def view_index(request, errors=None):
    """
    Landing page view.
    """
    if not errors:
        errors = []

    template = loader.get_template("site/index.html")

    recent = {
        "net": Network.handleref.filter(status="ok").order_by("-updated")[:5],
        "fac": Facility.handleref.filter(status="ok").order_by("-updated")[:5],
        "ix": InternetExchange.handleref.filter(status="ok").order_by("-updated")[:5],
    }

    env = BASE_ENV.copy()
    env.update({"errors": errors, "global_stats": global_stats(), "recent": recent})

    update_env_beta_sync_dt(env)

    return HttpResponse(template.render(env, request))


def view_component(
    request, component, data, title, perms=None, instance=None, **kwargs
):
    """
    Generic component view.
    """
    if not perms:
        perms = {}

    template = loader.get_template("site/view.html")

    env = BASE_ENV.copy()
    env.update(
        {
            "data": data,
            "permissions": perms,
            "title": title,
            "component": component,
            "instance": instance,
            "ref_tag": instance._handleref.tag,
            "global_stats": global_stats(),
            "asset_template_name": "site/view_%s_assets.html" % component,
            "tools_template_name": "site/view_%s_tools.html" % component,
            "side_template_name": "site/view_%s_side.html" % component,
            "bottom_template_name": "site/view_%s_bottom.html" % component,
        }
    )

    update_env_beta_sync_dt(env)

    env.update(**kwargs)
    return HttpResponse(template.render(env, request))


class OrganizationLogoUpload(View):

    """
    Handles public upload and setting of organization logo (#346)
    """

    @transaction.atomic
    def post(self, request, id):

        """upload and set a new logo"""

        org = Organization.objects.get(pk=id)

        # keep reference to current logo as we will need
        # to remove it after the new logo has been uploaded
        if org.logo:
            old_file = org.logo.path
        else:
            old_file = None

        # require update permissions to the org
        if not check_permissions(request.user, org, "u"):
            return JsonResponse({}, status=403)

        form = OrganizationLogoUploadForm(request.POST, request.FILES, instance=org)

        if form.is_valid():
            form.save()
            org.refresh_from_db()

            # remove old file if it exists
            if old_file and os.path.exists(old_file):
                os.remove(old_file)

            return JsonResponse({"status": "ok", "url": org.logo.url})
        else:
            return JsonResponse(form.errors, status=400)

    @transaction.atomic
    def delete(self, request, id):

        """delete the logo"""

        org = Organization.objects.get(pk=id)

        # require update permissions to the org
        if not check_permissions(request.user, org, "u"):
            return JsonResponse({}, status=403)

        org.logo.delete()

        return JsonResponse({"status": "ok"})


@ensure_csrf_cookie
def view_organization(request, id):
    """
    View organization data for org specified by id.
    """

    try:
        org = OrganizationSerializer.prefetch_related(
            Organization.objects, request, depth=2
        ).get(id=id, status="ok")
    except ObjectDoesNotExist:
        return view_http_error_404(request)

    data = OrganizationSerializer(org, context={"user": request.user}).data

    if not data:
        return view_http_error_403(request)

    perms = export_permissions(request.user, org)

    tags = ["fac", "net", "ix"]
    for tag in tags:
        model = REFTAG_MAP.get(tag)
        perms["can_create_%s" % tag] = check_permissions(
            request.user, model.Grainy.namespace_instance("*", org=org), PERM_CREATE
        )
        perms["can_delete_%s" % tag] = check_permissions(
            request.user, model.Grainy.namespace_instance("*", org=org), PERM_DELETE
        )

    # if the organization being viewed is the one used
    # to store suggested entities, we don't want to show the editorial
    # tools
    if org.id == dj_settings.SUGGEST_ENTITY_ORG:
        perms["can_create"] = False
        perms["can_manage"] = False
        for tag in tags:
            perms["can_create_%s" % tag] = False
            perms["can_delete_%s" % tag] = False

    # if user has writing perms to entity, we want to load sub entities
    # that have status pending so we don't use the ones kicked back
    # by the serializer
    if perms.get("can_delete_ix") or perms.get("can_create_ix"):
        exchanges = org.ix_set.filter(status__in=["ok", "pending"])
    else:
        exchanges = data["ix_set"]

    if perms.get("can_delete_fac") or perms.get("can_create_fac"):
        facilities = org.fac_set.filter(status__in=["ok", "pending"])
    else:
        facilities = data["fac_set"]

    if perms.get("can_delete_net") or perms.get("can_create_net"):
        networks = org.net_set.filter(status__in=["ok", "pending"])
    else:
        networks = data["net_set"]

    dismiss = DoNotRender()

    # determine if logo is specified and set the
    # logo url accordingly

    if org.logo:
        logo_url = org.logo.url
    else:
        logo_url = ""

    data = {
        "title": data.get("name", dismiss),
        "exchanges": exchanges,
        "networks": networks,
        "facilities": facilities,
        "fields": [
            {
                "name": "aka",
                "label": _("Also Known As"),
                "value": data.get("aka", dismiss),
                "notify_incomplete": False,
            },
            {
                "name": "name_long",
                "label": _("Long Name"),
                "value": data.get("name_long", dismiss),
                "notify_incomplete": False,
            },
            {
                "name": "website",
                "type": "url",
                "notify_incomplete": True,
                "value": data.get("website", dismiss),
                "label": _("Website"),
            },
            {
                "name": "address1",
                "label": _("Address 1"),
                "notify_incomplete": True,
                "value": data.get("address1", dismiss),
            },
            {
                "name": "address2",
                "label": _("Address 2"),
                "value": data.get("address2", dismiss),
            },
            {"name": "floor", "label": _("Floor"), "value": data.get("floor", dismiss)},
            {"name": "suite", "label": _("Suite"), "value": data.get("suite", dismiss)},
            {
                "name": "location",
                "label": _("Location"),
                "type": "location",
                "notify_incomplete": True,
                "value": data,
            },
            {
                "name": "country",
                "type": "list",
                "data": "countries_b",
                "label": _("Country Code"),
                "notify_incomplete": True,
                "value": data.get("country", dismiss),
            },
            {
                "name": "geocode",
                "label": _("Geocode"),
                "type": "geocode",
                "value": data,
            },
            {
                "readonly": True,
                "name": "updated",
                "label": _("Last Updated"),
                "value": data.get("updated", dismiss),
            },
            {
                "name": "notes",
                "label": _("Notes"),
                "help_text": _("Markdown enabled"),
                "type": "fmt-text",
                "value": data.get("notes", dismiss),
            },
            {
                "name": "logo",
                "label": _("Logo"),
                "help_text": field_help(Organization, "logo")
                + " - "
                + _("Max size: {}kb").format(int(dj_settings.ORG_LOGO_MAX_SIZE / 1024)),
                "type": "image",
                "accept": dj_settings.ORG_LOGO_ALLOWED_FILE_TYPE,
                "max_height": dj_settings.ORG_LOGO_MAX_VIEW_HEIGHT,
                "max_size": dj_settings.ORG_LOGO_MAX_SIZE,
                "upload_handler": f"/org/{org.id}/upload-logo",
                "value": logo_url,
            },
        ],
    }

    users = {}
    if perms.get("can_manage"):
        users.update({user.id: user for user in org.admin_usergroup.user_set.all()})
        users.update({user.id: user for user in org.usergroup.user_set.all()})
        users = sorted(list(users.values()), key=lambda x: x.full_name)

    # if user has rights to create sub entties or manage users, allow them
    # to view the tools
    if perms.get("can_manage") or perms.get("can_create"):
        perms["can_use_tools"] = True

    active_tab = None
    tab_init = {}
    for tag in tags:
        tab_init[tag] = "inactive"
        if perms.get("can_create_%s" % tag):
            perms["can_use_tools"] = True
            if not active_tab:
                tab_init[tag] = "active"
                active_tab = tag
        if perms.get("can_delete_%s" % tag):
            perms["can_edit"] = True

    if perms.get("can_manage") and org.pending_affiliations.count() > 0:
        tab_init = {"users": "active"}

    if request.GET.get("tab"):
        tab_init = {request.GET.get("tab"): "active"}

    keys = [
        {"prefix": key.prefix, "hashed_key": key.hashed_key, "name": key.name}
        for key in org.api_keys.filter(revoked=False).all()
    ]
    data["phone_help_text"] = field_help(NetworkContact, "phone")

    return view_component(
        request,
        "organization",
        data,
        "Organization",
        tab_init=tab_init,
        users=users,
        user_perms=load_all_user_permissions(org),
        key_perms=load_all_key_permissions(org),
        instance=org,
        perms=perms,
        keys=keys,
    )


@ensure_csrf_cookie
def view_facility(request, id):
    """
    View facility data for facility specified by id.
    """

    try:
        facility = Facility.objects.get(id=id, status="ok")
    except ObjectDoesNotExist:
        return view_http_error_404(request)

    data = FacilitySerializer(facility, context={"user": request.user}).data

    applicator = APIPermissionsApplicator(request)

    if not applicator.is_generating_api_cache:
        data = applicator.apply(data)

    if not data:
        return view_http_error_403(request)

    perms = export_permissions(request.user, facility)

    org = OrganizationSerializer(facility.org, context={"user": request.user}).data

    exchanges = (
        InternetExchangeFacility.handleref.undeleted()
        .filter(facility=facility)
        .select_related("ix")
        .order_by("ix__name")
        .all()
    )
    peers = (
        NetworkFacility.handleref.undeleted()
        .filter(facility=facility)
        .select_related("network")
        .order_by("network__name")
    )

    if facility.org.logo:
        org["logo"] = facility.org.logo.url

    dismiss = DoNotRender()

    data = {
        "title": data.get("name", dismiss),
        "exchanges": exchanges,
        "peers": peers,
        "fields": [
            {
                "name": "org",
                "label": _("Organization"),
                "value": org.get("name", dismiss),
                "type": "entity_link",
                "link": "/%s/%d" % (Organization._handleref.tag, org.get("id")),
            },
            {
                "name": "aka",
                "label": _("Also Known As"),
                "value": data.get("aka", dismiss),
                "notify_incomplete": False,
            },
            {
                "name": "name_long",
                "label": _("Long Name"),
                "value": data.get("name_long", dismiss),
                "notify_incomplete": False,
            },
            {
                "name": "website",
                "type": "url",
                "value": data.get("website", dismiss),
                "label": _("Website"),
            },
            {
                "name": "address1",
                "label": _("Address 1"),
                "value": data.get("address1", dismiss),
            },
            {
                "name": "address2",
                "label": _("Address 2"),
                "value": data.get("address2", dismiss),
            },
            {"name": "floor", "label": _("Floor"), "value": data.get("floor", dismiss)},
            {"name": "suite", "label": _("Suite"), "value": data.get("suite", dismiss)},
            {
                "name": "location",
                "label": _("Location"),
                "type": "location",
                "value": data,
            },
            {
                "name": "country",
                "type": "list",
                "data": "countries_b",
                "label": _("Country Code"),
                "value": data.get("country", dismiss),
            },
            {
                "name": "region_continent",
                "type": "list",
                "data": "enum/regions",
                "label": _("Continental Region"),
                "value": data.get("region_continent", dismiss),
                "readonly": True,
            },
            {
                "name": "geocode",
                "label": _("Geocode"),
                "type": "geocode",
                "value": data,
            },
            {
                "name": "clli",
                "label": _("CLLI Code"),
                "value": data.get("clli", dismiss),
            },
            {
                "name": "npanxx",
                "label": _("NPA-NXX"),
                "value": data.get("npanxx", dismiss),
            },
            {
                "readonly": True,
                "name": "updated",
                "label": _("Last Updated"),
                "value": data.get("updated", dismiss),
            },
            {
                "name": "notes",
                "label": _("Notes"),
                "help_text": _("Markdown enabled"),
                "type": "fmt-text",
                "value": data.get("notes", dismiss),
            },
            {
                "name": "org_logo",
                "label": "",
                "value": org.get("logo", dismiss),
                "type": "image",
                "readonly": True,
                "max_height": dj_settings.ORG_LOGO_MAX_VIEW_HEIGHT,
            },
            {
                "type": "email",
                "name": "tech_email",
                "label": _("Technical Email"),
                "value": data.get("tech_email", dismiss),
            },
            {
                "type": "string",
                "name": "tech_phone",
                "label": _("Technical Phone"),
                "value": data.get("tech_phone", dismiss),
                "help_text": field_help(Facility, "tech_phone"),
            },
            {
                "type": "email",
                "name": "sales_email",
                "label": _("Sales Email"),
                "value": data.get("sales_email", dismiss),
            },
            {
                "type": "string",
                "name": "sales_phone",
                "label": _("Sales Phone"),
                "value": data.get("sales_phone", dismiss),
                "help_text": field_help(Facility, "sales_phone"),
            },
            {
                "name": "property",
                "type": "list",
                "data": "enum/property",
                "label": _("Property"),
                "value": data.get("property", dismiss),
                "help_text": field_help(Facility, "property"),
            },
            {
                "name": "diverse_serving_substations",
                "type": "list",
                "data": "enum/bool_choice_with_opt_out_str",
                "label": _("Diverse Serving Substations"),
                "value": data.get("diverse_serving_substations", dismiss),
                "value_label": dict(BOOL_CHOICE_WITH_OPT_OUT).get(
                    data.get("diverse_serving_substations")
                ),
                "help_text": field_help(Facility, "diverse_serving_substations"),
            },
            {
                "name": "available_voltage_services",
                "type": "list",
                "multiple": True,
                "data": "enum/available_voltage",
                "label": _("Available Voltage Services"),
                "value": data.get("available_voltage_services", dismiss),
                "help_text": field_help(Facility, "available_voltage_services"),
            },
            {
                "name": "status_dashboard",
                "type": "url",
                "value": data.get("status_dashboard", dismiss),
                "label": _("Health Check"),
            },
        ],
    }

    data["stats"] = get_fac_stats(peers, exchanges)

    return view_component(
        request, "facility", data, "Facility", perms=perms, instance=facility
    )


@ensure_csrf_cookie
def view_exchange(request, id):
    """
    View exchange data for exchange specified by id.
    """

    try:
        exchange = InternetExchange.objects.get(id=id, status="ok")
    except ObjectDoesNotExist:
        return view_http_error_404(request)

    data = InternetExchangeSerializer(exchange, context={"user": request.user}).data

    applicator = APIPermissionsApplicator(request)

    if not applicator.is_generating_api_cache:
        data = applicator.apply(data)

    # find out if user can write to object
    perms = export_permissions(request.user, exchange)

    if not data:
        return view_http_error_403(request)
    networks = (
        NetworkIXLan.handleref.undeleted()
        .select_related("network", "ixlan")
        .order_by("network__name")
        .filter(ixlan__ix=exchange)
    )
    dismiss = DoNotRender()

    facilities = (
        InternetExchangeFacility.handleref.undeleted()
        .select_related("ix", "facility")
        .filter(ix=exchange)
        .order_by("facility__name")
    )

    org = data.get("org")

    if exchange.org.logo:
        org["logo"] = exchange.org.logo.url

    data = {
        "id": exchange.id,
        "title": data.get("name", dismiss),
        "facilities": facilities,
        "networks": networks,
        "peer_count": 0,
        "connections_count": 0,
        "open_peer_count": 0,
        "total_speed": 0,
        "ipv6_percentage": 0,
        "ixlans": exchange.ixlan_set_active_or_pending,
        "fields": [
            {
                "name": "org",
                "label": _("Organization"),
                "value": org.get("name", dismiss),
                "type": "entity_link",
                "link": "/%s/%d" % (Organization._handleref.tag, org.get("id")),
            },
            {
                "name": "aka",
                "label": _("Also Known As"),
                "value": data.get("aka", dismiss),
            },
            {
                "name": "name_long",
                "label": _("Long Name"),
                "value": data.get("name_long", dismiss),
            },
            {"name": "city", "label": _("City"), "value": data.get("city", dismiss)},
            {
                "name": "country",
                "type": "list",
                "data": "countries_b",
                "label": _("Country"),
                "value": data.get("country", dismiss),
            },
            {
                "name": "region_continent",
                "type": "list",
                "data": "enum/regions",
                "label": _("Continental Region"),
                "value": data.get("region_continent", dismiss),
            },
            {
                "name": "media",
                "type": "list",
                "data": "enum/media",
                "label": _("Media Type"),
                "value": data.get("media", dismiss),
            },
            {
                "name": "service_level",
                "type": "list",
                "data": "enum/service_level_types_trunc",
                "label": _("Service Level"),
                "value": data.get("service_level", dismiss),
            },
            {
                "name": "terms",
                "type": "list",
                "data": "enum/terms_types_trunc",
                "label": _("Terms"),
                "value": data.get("terms", dismiss),
            },
            {
                "readonly": True,
                "name": "updated",
                "label": _("Last Updated"),
                "value": data.get("updated", dismiss),
            },
            {
                "name": "notes",
                "label": _("Notes"),
                "help_text": _("Markdown enabled"),
                "type": "fmt-text",
                "value": data.get("notes", dismiss),
            },
            {
                "name": "org_logo",
                "label": "",
                "value": org.get("logo", dismiss),
                "type": "image",
                "readonly": True,
                "max_height": dj_settings.ORG_LOGO_MAX_VIEW_HEIGHT,
            },
            {"type": "sub", "label": _("Contact Information")},
            {
                "type": "url",
                "name": "website",
                "label": _("Company Website"),
                "value": data.get("website", dismiss),
            },
            {
                "type": "url",
                "name": "url_stats",
                "label": _("Traffic Stats Website"),
                "value": data.get("url_stats", dismiss),
            },
            {
                "type": "email",
                "name": "tech_email",
                "label": _("Technical Email"),
                "value": data.get("tech_email", dismiss),
            },
            {
                "type": "string",
                "name": "tech_phone",
                "label": _("Technical Phone"),
                "value": data.get("tech_phone", dismiss),
                "help_text": field_help(InternetExchange, "tech_phone"),
            },
            {
                "type": "email",
                "name": "policy_email",
                "label": _("Policy Email"),
                "value": data.get("policy_email", dismiss),
            },
            {
                "type": "string",
                "name": "policy_phone",
                "label": _("Policy Phone"),
                "value": data.get("policy_phone", dismiss),
                "help_text": field_help(InternetExchange, "policy_phone"),
            },
            {
                "type": "email",
                "name": "sales_email",
                "label": _("Sales Email"),
                "value": data.get("sales_email", dismiss),
            },
            {
                "type": "string",
                "name": "sales_phone",
                "label": _("Sales Phone"),
                "value": data.get("sales_phone", dismiss),
                "help_text": field_help(InternetExchange, "sales_phone"),
            },
            {
                "type": "url",
                "name": "status_dashboard",
                "label": _("Health Check"),
                "value": data.get("status_dashboard", dismiss),
            },
        ],
    }

    # IXLAN field group (form)

    ixlan = exchange.ixlan

    data["fields"].extend(
        [
            {
                "type": "group",
                "target": "api:ixlan:update",
                "id": ixlan.id,
                "label": _("LAN"),
                "payload": [{"name": "ix_id", "value": exchange.id}],
            },
            {
                "type": "number",
                "name": "mtu",
                "label": _("MTU"),
                "value": (ixlan.mtu or 0),
            },
            {
                "type": "flags",
                "label": _("Enable IX-F Import"),
                "value": [
                    {
                        "name": "ixf_ixp_import_enabled",
                        "value": ixlan.ixf_ixp_import_enabled,
                    }
                ],
                "admin": True,
            },
            {
                "type": "url",
                "label": _("IX-F Member Export URL"),
                "name": "ixf_ixp_member_list_url",
                "value": DoNotRender.permissioned(
                    ixlan.ixf_ixp_member_list_url,
                    request.user,
                    f"{ixlan.grainy_namespace}.ixf_ixp_member_list_url"
                    f".{ixlan.ixf_ixp_member_list_url_visible}",
                    explicit=True,
                ),
            },
            {
                "type": "list",
                "name": "ixf_ixp_member_list_url_visible",
                "data": "enum/visibility",
                "label": _("IX-F Member Export URL Visibility"),
                "value": ixlan.ixf_ixp_member_list_url_visible,
            },
            {
                "type": "action",
                "label": _("IX-F Import Preview"),
                "actions": [{"label": _("Preview"), "action": "ixf_preview"}],
                "admin": True,
            },
            {
                "type": "action",
                "label": _("IX-F Import"),
                "actions": [
                    {"label": _("Request import"), "action": "ixf_request_import"},
                    {
                        "label": exchange.ixf_import_request_recent_status[1],
                        "css": f"ixf-import-request-status {exchange.ixf_import_css}",
                    },
                ],
                "admin": True,
            },
            {"type": "group_end"},
        ]
    )

    data["stats"] = get_ix_stats(networks, ixlan)

    return view_component(
        request, "exchange", data, "Exchange", perms=perms, instance=exchange
    )


@login_required
def watch_network(request, id):

    """
    Adds data-change notifications for the specified network (id)
    for the rquesting user.

    User needs write permissions to the network to be eligible for data change
    notifications.
    """

    # make sure network exists
    net = Network.objects.get(id=id)

    if not check_permissions(request.user, net, PERM_CREATE):
        return HttpResponse(status=403)

    # add watched status
    DataChangeWatchedObject.objects.get_or_create(
        user=request.user, ref_tag="net", object_id=id
    )

    return redirect(reverse("net-view", args=(id,)))


@login_required
def unwatch_network(request, id):

    # make sure network exists
    net = Network.objects.get(id=id)

    if not check_permissions(request.user, net, PERM_CREATE):
        return HttpResponse(status=403)

    # remove watched status
    DataChangeWatchedObject.objects.filter(
        user=request.user, ref_tag="net", object_id=id
    ).delete()

    return redirect(reverse("net-view", args=(id,)))


@ensure_csrf_cookie
def view_network_by_query(request):
    if "asn" in request.GET:
        try:
            return view_network_by_asn(request, request.GET.get("asn"))
        except ValueError:
            return view_http_error_404(request)
    else:
        return view_http_error_404(request)


@ensure_csrf_cookie
def view_network_by_asn(request, asn):
    try:
        network = Network.objects.get(asn=int(asn))
        # FIXME: should be able to just pass existing network object here to avoid
        # having to query again
        return view_network(request, network.id)
    except ObjectDoesNotExist:
        return view_http_error_404(request)


def format_last_updated_time(last_updated_time):
    if last_updated_time is None:
        return ""
    elif isinstance(last_updated_time, str):
        return last_updated_time.split(".")[0]


@ensure_csrf_cookie
def view_network(request, id):
    """
    View network data for network specified by id.
    """
    try:
        network = NetworkSerializer.prefetch_related(
            Network.objects, request, depth=2, selective=["poc_set"]
        ).get(id=id, status="ok")
    except ObjectDoesNotExist:
        return view_http_error_404(request)

    network_d = NetworkSerializer(network, context={"user": request.user}).data
    applicator = APIPermissionsApplicator(request)

    if not applicator.is_generating_api_cache:
        network_d = applicator.apply(network_d)

    if not network_d:
        return view_http_error_403(request)

    perms = export_permissions(request.user, network)

    facilities = (
        NetworkFacility.handleref.undeleted()
        .select_related("facility")
        .filter(network=network)
        .order_by("facility__name")
    )

    exchanges = (
        NetworkIXLan.handleref.undeleted()
        .select_related("ixlan", "ixlan__ix", "network")
        .filter(network=network)
        .order_by("ixlan__ix__name")
    )

    # This will be passed as default value for keys that don't exist - causing
    # them not to be rendered in the template - also it is fairly
    # safe to assume that no existing keys have been dropped because permission
    # requirements to view them were not met.
    dismiss = DoNotRender()

    org = network_d.get("org")

    ixf_proposals = IXFMemberData.proposals_for_network(network)
    ixf_proposals_dismissed = IXFMemberData.network_has_dismissed_actionable(network)

    if network.org.logo:
        org["logo"] = network.org.logo.url

    if DataChangeWatchedObject.watching(request.user, network):
        watch_actions = [
            {
                "label": _("Disable notifications"),
                "href": reverse("net-unwatch", args=(network.id,)),
            }
        ]
    else:
        watch_actions = [
            {
                "label": _("Enable notifications"),
                "href": reverse("net-watch", args=(network.id,)),
            }
        ]

    data = {
        "title": network_d.get("name", dismiss),
        "facilities": facilities,
        "exchanges": exchanges,
        "ixf": ixf_proposals,
        "ixf_dismissed": ixf_proposals_dismissed,
        "fields": [
            {
                "name": "org",
                "label": _("Organization"),
                "value": org.get("name", dismiss),
                "type": "entity_link",
                "link": "/%s/%d" % (Organization._handleref.tag, org.get("id")),
            },
            {
                "name": "aka",
                "label": _("Also Known As"),
                "notify_incomplete": False,
                "value": network_d.get("aka", dismiss),
            },
            {
                "name": "name_long",
                "label": _("Long Name"),
                "notify_incomplete": False,
                "value": network_d.get("name_long", dismiss),
            },
            {
                "name": "website",
                "label": _("Company Website"),
                "type": "url",
                "notify_incomplete": True,
                "value": network_d.get("website", dismiss),
            },
            {
                "name": "asn",
                "label": _("ASN"),
                "notify_incomplete": True,
                "value": network_d.get("asn", dismiss),
                "readonly": True,
            },
            {
                "name": "irr_as_set",
                "label": _("IRR as-set/route-set"),
                "help_text": field_help(Network, "irr_as_set"),
                "notify_incomplete": True,
                "value": network_d.get("irr_as_set", dismiss),
            },
            {
                "name": "route_server",
                "type": "url",
                "label": _("Route Server URL"),
                "notify_incomplete": False,
                "value": network_d.get("route_server", dismiss),
            },
            {
                "name": "looking_glass",
                "type": "url",
                "label": _("Looking Glass URL"),
                "notify_incomplete": False,
                "value": network_d.get("looking_glass", dismiss),
            },
            {
                "name": "info_type",
                "type": "list",
                "data": "enum/net_types",
                "blank": _("Not Disclosed"),
                "label": _("Network Type"),
                "notify_incomplete": True,
                "value": network_d.get("info_type", dismiss),
            },
            {
                "name": "info_prefixes4",
                "label": _("IPv4 Prefixes"),
                "type": "number",
                "help_text": field_help(Network, "info_prefixes4"),
                "notify_incomplete": True,
                "notify_incomplete_group": "prefixes",
                "value": int(network_d.get("info_prefixes4") or 0),
            },
            {
                "name": "info_prefixes6",
                "label": _("IPv6 Prefixes"),
                "type": "number",
                "help_text": field_help(Network, "info_prefixes6"),
                "notify_incomplete": True,
                "notify_incomplete_group": "prefixes",
                "value": int(network_d.get("info_prefixes6") or 0),
            },
            {
                "name": "info_traffic",
                "type": "list",
                "data": "enum/traffic",
                "blank": _("Not Disclosed"),
                "label": _("Traffic Levels"),
                "value": network_d.get("info_traffic", dismiss),
            },
            {
                "name": "info_ratio",
                "type": "list",
                "data": "enum/ratios",
                "label": _("Traffic Ratios"),
                "blank": _("Not Disclosed"),
                "value": network_d.get("info_ratio", dismiss),
            },
            {
                "name": "info_scope",
                "type": "list",
                "data": "enum/scopes",
                "blank": _("Not Disclosed"),
                "label": _("Geographic Scope"),
                "value": network_d.get("info_scope", dismiss),
            },
            {
                "type": "flags",
                "label": _("Protocols Supported"),
                "value": [
                    {
                        "name": "info_unicast",
                        "label": _("Unicast IPv4"),
                        "value": network_d.get("info_unicast", False),
                    },
                    {
                        "name": "info_multicast",
                        "label": _("Multicast"),
                        "value": network_d.get("info_multicast", False),
                    },
                    {
                        "name": "info_ipv6",
                        "label": _("IPv6"),
                        "value": network_d.get("info_ipv6", False),
                    },
                    {
                        "name": "info_never_via_route_servers",
                        "label": _("Never via route servers"),
                        "help_text": field_help(
                            Network, "info_never_via_route_servers"
                        ),
                        "value": network_d.get("info_never_via_route_servers", False),
                    },
                ],
            },
            {
                "readonly": True,
                "name": "updated",
                "label": _("Last Updated"),
                "value": format_last_updated_time(network_d.get("updated")),
            },
            {
                "readonly": True,
                "name": "netixlan_updated",
                "label": _("Public Peering Info Updated"),
                "value": format_last_updated_time(network_d.get("netixlan_updated")),
            },
            {
                "readonly": True,
                "name": "netfac_updated",
                "label": _("Peering Facility Info Updated"),
                "value": format_last_updated_time(network_d.get("netfac_updated")),
            },
            {
                "readonly": True,
                "name": "poc_updated",
                "label": _("Contact Info Updated"),
                "value": format_last_updated_time(network_d.get("poc_updated")),
            },
            {
                "name": "notes",
                "label": _("Notes"),
                "help_text": _("Markdown enabled"),
                "type": "fmt-text",
                "value": network_d.get("notes", dismiss),
            },
            {
                "name": "rir_status",
                "readonly": True,
                "label": _("RIR Status"),
                "type": "fmt-text",
                "value": network_d.get("rir_status", dismiss),
            },
            {
                "name": "rir_status_updated",
                "readonly": True,
                "label": _("RIR Status Updated"),
                "type": "fmt-text",
                "value": format_last_updated_time(network_d.get("rir_status_updated")),
            },
            {
                "name": "org_logo",
                "label": "",
                "value": org.get("logo", dismiss),
                "type": "image",
                "readonly": True,
                "max_height": dj_settings.ORG_LOGO_MAX_VIEW_HEIGHT,
            },
            {"type": "sub", "admin": True, "label": _("PeeringDB Configuration")},
            {
                "type": "flags",
                "admin": True,
                "label": _("Allow IXP Update"),
                "help_text": _(
                    "If enabled, an IXP may manage this network's entry in their peering list"
                ),
                "value": [
                    {
                        "name": "allow_ixp_update",
                        "label": "",
                        "value": network.allow_ixp_update,
                    }
                ],
            },
            {
                "type": "action",
                "admin": True,
                "label": _("Notify On IXP Update"),
                "help_text": _(
                    "Notify me when an IXP modifies the peering exchange points for this network"
                ),
                "actions": watch_actions,
            },
            {
                "type": "action",
                "admin": True,
                "label": _("IXP Update Tools"),
                "actions": [
                    {"label": _("Preview"), "action": "ixf_preview"},
                    {"label": _("Postmortem"), "action": "ixf_postmortem"},
                ],
            },
            {"type": "sub", "label": _("Peering Policy Information")},
            {
                "name": "policy_url",
                "label": _("Peering Policy"),
                "value": network_d.get("policy_url", dismiss),
                "notify_incomplete": True,
                "type": "url",
            },
            {
                "name": "policy_general",
                "type": "list",
                "data": "enum/policy_general",
                "label": _("General Policy"),
                "value": network_d.get("policy_general", dismiss),
            },
            {
                "name": "policy_locations",
                "type": "list",
                "data": "enum/policy_locations",
                "label": _("Multiple Locations"),
                "value": network_d.get("policy_locations", dismiss),
            },
            {
                "name": "policy_ratio",
                "type": "list",
                "data": "enum/bool_choice_str",
                "label": _("Ratio Requirement"),
                "value": network_d.get("policy_ratio", dismiss),
                "value_label": dict(BOOL_CHOICE).get(network_d.get("policy_ratio")),
            },
            {
                "name": "policy_contracts",
                "type": "list",
                "data": "enum/policy_contracts",
                "label": _("Contract Requirement"),
                "value": network_d.get("policy_contracts", dismiss),
            },
            {
                "name": "status_dashboard",
                "label": _("Health Check"),
                "value": network_d.get("status_dashboard", dismiss),
                "type": "url",
            },
        ],
    }

    # Add POC data to dataset
    data["poc_set"] = network_d.get("poc_set")

    # For tooltip
    data["phone_help_text"] = field_help(NetworkContact, "phone")

    if not request.user.is_authenticated or not request.user.is_verified_user:
        cnt = network.poc_set.filter(status="ok", visible="Users").count()
        data["poc_hidden"] = cnt > 0
    else:
        data["poc_hidden"] = False

    return view_component(
        request, "network", data, "Network", perms=perms, instance=network
    )


def view_suggest(request, reftag):
    if reftag not in ["net", "ix", "fac"]:
        return HttpResponseRedirect("/")

    template = loader.get_template(f"site/view_suggest_{reftag}.html")
    env = make_env()

    env["phone_help_text"] = field_help(NetworkContact, "phone")
    return HttpResponse(template.render(env, request))


def view_simple_content(request, content_name):
    """
    Render the content in templates/{{ content_name }} inside
    the peeringdb layout.
    """

    template = loader.get_template("site/simple_content.html")

    env = make_env(content_name=content_name)

    return HttpResponse(template.render(env, request))


def view_aup(request):
    """
    Render page containing acceptable use policy.
    """

    return view_simple_content(request, "site/aup.html")


def view_about(request):
    """
    Render page containing about.
    """

    return view_simple_content(request, "site/about.html")


def view_sponsorships(request):
    """
    View current sponsorships.
    """

    template = loader.get_template("site/sponsorships.html")
    now = datetime.datetime.now().replace(tzinfo=UTC())

    qset = Sponsorship.objects.filter(start_date__lte=now, end_date__gte=now)

    sponsorships = {
        "diamond": qset.filter(level=4),
        "platinum": qset.filter(level=3),
        "gold": qset.filter(level=2),
        "silver": qset.filter(level=1),
    }

    env = make_env(sponsorships=sponsorships)
    return HttpResponse(template.render(env, request))


def view_partnerships(request):
    """
    View current partners.
    """

    template = loader.get_template("site/partnerships.html")
    qset = Partnership.objects.filter(logo__isnull=False)

    partnerships = {}
    for row in qset:
        if row.level not in partnerships:
            partnerships[row.level] = []
        partnerships[row.level].append(row)

    env = make_env(
        partnership_levels=dict(PARTNERSHIP_LEVELS), partnerships=partnerships
    )
    return HttpResponse(template.render(env, request))


def view_advanced_search(request):
    """
    View for advanced search.
    """

    template = loader.get_template("site/advanced-search.html")
    env = make_env(row_limit=getattr(dj_settings, "API_DEPTH_ROW_LIMIT", 250))

    reftag = request.GET.get("reftag")

    if reftag == "net":
        try:
            env["ix_name"] = InternetExchange.objects.get(id=request.GET.get("ix")).name
        except (ObjectDoesNotExist, ValueError):
            env["ix_name"] = ""

        try:
            env["not_ix_name"] = InternetExchange.objects.get(
                id=request.GET.get("not_ix")
            ).name
        except (ObjectDoesNotExist, ValueError):
            env["not_ix_name"] = ""

        try:
            env["fac_name"] = Facility.objects.get(id=request.GET.get("fac")).name
        except (ObjectDoesNotExist, ValueError):
            env["fac_name"] = ""

        try:
            env["not_fac_name"] = Facility.objects.get(
                id=request.GET.get("not_fac")
            ).name
        except (ObjectDoesNotExist, ValueError):
            env["not_fac_name"] = ""

    env["can_use_distance_filter"] = (
        dj_settings.API_DISTANCE_FILTER_REQUIRE_AUTH is False
        or request.user.is_authenticated
    ) and (
        dj_settings.API_DISTANCE_FILTER_REQUIRE_VERIFIED is False
        or (request.user.is_authenticated and request.user.is_verified_user)
    )

    return HttpResponse(template.render(env, request))


def request_api_search(request):
    """
    Triggered by typing something in the main peeringdb search bar
    without hitting enter (quasi autocomplete).
    """

    q = request.GET.get("q")

    if not q:
        return HttpResponseBadRequest()

    result = search(q, autocomplete=True)

    return HttpResponse(json.dumps(result), content_type="application/json")


def request_search(request):
    """
    Triggered by hitting enter on the main search bar.
    Renders a search result page.
    """
    q = request.GET.get("q")

    if not q:
        return HttpResponseRedirect("/")

    # if the user queried for an asn directly via AS*** or ASN***
    # redirect to the result
    m = re.match(r"(asn|as)(\d+)", q.lower())
    if m:
        net = Network.objects.filter(asn=m.group(2), status="ok")
        if net.exists() and net.count() == 1:
            return HttpResponseRedirect(f"/net/{net.first().id}")

    result = search(q)

    sponsors = {
        org.id: {"label": sponsorship.label.lower(), "css": sponsorship.css}
        for org, sponsorship in Sponsorship.active_by_org()
    }

    for tag, rows in list(result.items()):
        for item in rows:
            item["sponsorship"] = sponsors.get(item["org_id"])

    template = loader.get_template("site/search_result.html")
    env = make_env(
        **{
            "search_ixp": result.get(InternetExchange._handleref.tag),
            "search_net": result.get(Network._handleref.tag),
            "search_fac": result.get(Facility._handleref.tag),
            "search_org": result.get(Organization._handleref.tag),
            "count_ixp": len(result.get(InternetExchange._handleref.tag, [])),
            "count_net": len(result.get(Network._handleref.tag, [])),
            "count_fac": len(result.get(Facility._handleref.tag, [])),
            "count_org": len(result.get(Organization._handleref.tag, [])),
        }
    )
    return HttpResponse(template.render(env, request))


@transaction.atomic
def request_logout(request):
    logout(request)
    return redirect("/")


# We are using django-otp's EmailDevice model
# to handle email as a recovery option for one
# time passwords.
#
# Unlike all the other devices supported by
# django-two-factor-auth it's token field is
# not an integer field. So the token to be verified
# needs to be turned into a string
#
# So we monkey patch it's verify_token function
# to do just that

EmailDevice._verify_token = EmailDevice.verify_token


def verify_token(self, token):
    return self._verify_token(str(token))


EmailDevice.verify_token = verify_token


class LoginView(TwoFactorLoginView):

    """
    Extend the `LoginView` class provided
    by `two_factor` because some
    PDB specific functionality and checks need to be added.
    """

    def get(self, *args, **kwargs):
        """
        If a user is already authenticated, don't show the
        login process, instead redirect to /
        """

        next_redirect = self.request.GET.get("next", "")
        if is_oauth_authorize(next_redirect):
            return super().get(*args, **kwargs)
        if self.request.user.is_authenticated:
            return redirect("/")

        return super().get(*args, **kwargs)

    def get_form_kwargs(self, step=None):
        kwargs = super().get_form_kwargs(step=step)

        if step == "security-key":
            kwargs.update(
                {"device": self.get_security_key_device(), "request": self.request}
            )

        return kwargs

    @transaction.atomic
    @method_decorator(
        ratelimit(key="ip", rate=RATELIMITS["request_login_POST"], method="POST")
    )
    def post(self, *args, **kwargs):
        """
        Posts to the `auth` step of the authentication
        process need to be rate limited.
        """
        request = self.request

        was_limited = getattr(self.request, "limited", False)
        if self.get_step_index() == 0 and was_limited:
            self.rate_limit_message = _(
                "Please wait a bit before trying to login again."
            )
            return self.render_goto_step("auth")

        passwordless = self.attempt_passwordless_auth(request, **kwargs)
        if passwordless:
            return passwordless

        return super().post(*args, **kwargs)

    def get_context_data(self, form, **kwargs):
        """
        If post request was rate limited the rate limit message
        needs to be communicated via the template context.
        """

        context = super().get_context_data(form, **kwargs)
        context.update(rate_limit_message=getattr(self, "rate_limit_message", None))

        # make_env results to context
        context.update(**make_env())

        if "other_devices" in context:
            email_device = self.get_email_device()

            # If the user has an email device, we need to
            # add the email device to the context
            if email_device:
                context["other_devices"] += [email_device]

        return context

    def get_email_device(self):
        """
        Return an EmailDevice instance for the requesting user
        which can be used for one time passwords.
        """

        user = self.get_user()

        if user.email_confirmed:

            # only users with confirmed emails should have
            # the option to request otp to their email address

            try:

                # check if user already has an EmailDevice instance

                device = EmailDevice.objects.get(user=user)

                if not device.confirmed:

                    # sync confirmed status

                    device.confirmed = True
                    device.save()
            except EmailDevice.DoesNotExist:

                # create EmaiLDevice object for user if it does
                # not exist

                device = EmailDevice.objects.create(user=user, confirmed=True)

            # django-two-factor-auth needs this property set to something
            device.method = "email"

            return device
        else:

            # if user does NOT have a confirmed email address but
            # somehow has an EmailDevice object, delete it.

            try:
                device = EmailDevice.objects.get(user=user)
                device.delete()
            except EmailDevice.DoesNotExist:
                pass

        return None

    def get_device(self, step=None):
        """
        Override this to can enable EmailDevice as a
        challenge device for one time passwords.
        """

        if not self.device_cache:
            challenge_device_id = self.request.POST.get("challenge_device", None)
            if challenge_device_id:

                # email device
                device = self.get_email_device()
                if device.persistent_id == challenge_device_id:
                    self.device_cache = device
                    return self.device_cache

        return super().get_device(step=step)

    def get_success_url(self):
        return self.get_redirect_url()

    def get_redirect_url(self):
        """
        Specify which redirect urls are valid.
        """

        redir = self.request.POST.get("next") or "/"

        # if the redirect url is to logout that makes little
        # sense as the user would get logged out immediately
        # after logging in, substitute with a redirect to `/` instead

        if redir == "/logout":
            redir = "/"

        # check if the redirect url can be resolved to a view
        # if yes, it's a valid redirect
        try:
            resolve(redir)
        except Resolver404:

            # url could not be resolved to a view, so it's likely
            # invalid or pointing somewhere externally, the only
            # external urls we want to allow are the redirect urls
            # of oauth applications set up in peeringdb

            if not is_oauth_authorize(redir):
                redir = "/"

        return redir

    def done(self, form_list, **kwargs):
        """
        User authenticated successfully, set language options.
        """

        super().done(form_list, **kwargs)

        # TODO: do this via signal instead?

        user_language = self.get_user().get_locale()
        translation.activate(user_language)
        success_url = self.get_success_url()
        response = redirect(self.get_success_url())
        if is_oauth_authorize(success_url):
            response.set_signed_cookie(
                "oauth_session",
                self.request.user,
                max_age=dj_settings.OAUTH_COOKIE_MAX_AGE,
            )
        response.set_cookie(dj_settings.LANGUAGE_COOKIE_NAME, user_language)

        return response


@require_http_methods(["POST"])
@ratelimit(key="ip", rate=RATELIMITS["request_translation"], method="POST")
def request_translation(request, data_type):

    if not request.user.is_authenticated:
        return JsonResponse(
            {"status": "error", "error": "Please login to use translation service"}
        )

    user_language = request.user.get_locale()
    if not user_language:
        user_language = "en"

    note = request.POST.get("note")
    target = user_language

    if note and target:

        translationURL = "https://translation.googleapis.com/language/translate/v2"
        call_params = {
            "key": dj_settings.GOOGLE_GEOLOC_API_KEY,
            "q": note,
            "target": target,
        }
        reply = requests.post(translationURL, params=call_params).json()

        if "data" not in reply:
            return JsonResponse({"status": request.POST, "error": reply})

        return JsonResponse(
            {"status": request.POST, "translation": reply["data"]["translations"][0]}
        )

    return JsonResponse(
        {"status": "error", "error": "No text or no language specified"}
    )


@transaction.atomic
@require_http_methods(["POST"])
def network_reset_ixf_proposals(request, net_id):
    net = Network.objects.get(id=net_id)

    allowed = check_permissions(request.user, net, PERM_UPDATE)

    if not allowed:
        return JsonResponse({"non_field_errors": [_("Permission denied")]}, status=401)

    qset = IXFMemberData.objects.filter(asn=net.asn)
    qset.update(dismissed=False)

    return JsonResponse({"status": "ok"})


@transaction.atomic
@require_http_methods(["POST"])
def network_dismiss_ixf_proposal(request, net_id, ixf_id):
    ixf_member_data = IXFMemberData.objects.get(id=ixf_id)
    net = ixf_member_data.net

    allowed = check_permissions(request.user, net, PERM_UPDATE)

    if not allowed:
        return JsonResponse({"non_field_errors": [_("Permission denied")]}, status=401)

    ixf_member_data.dismissed = True
    ixf_member_data.save()

    return JsonResponse({"status": "ok"})


def validator_result_cache(request, cache_id):
    """
    Return CSV data from cache.
    """
    data = cache.get(cache_id)

    # If cache key doesn't exist, return 404
    # Prevent downloads from non-admin in users

    if not data or not request.user.is_superuser:
        return view_http_error_404(request)
    response = HttpResponse(
        content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{cache_id}"'},
    )
    response.write(data)
    return response
