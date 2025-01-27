import ast
import base64
import json
import re
from typing import Any

import flask
import jwt
from flask import current_app
from flask import g
from flask import jsonify
from flask import make_response
from flask import redirect
from flask import request
from werkzeug.wrappers import Response

from spiffworkflow_backend.exceptions.api_error import ApiError
from spiffworkflow_backend.exceptions.error import InvalidRedirectUrlError
from spiffworkflow_backend.exceptions.error import MissingAccessTokenError
from spiffworkflow_backend.exceptions.error import TokenExpiredError
from spiffworkflow_backend.helpers.api_version import V1_API_PATH_PREFIX
from spiffworkflow_backend.models.group import SPIFF_GUEST_GROUP
from spiffworkflow_backend.models.group import SPIFF_NO_AUTH_GROUP
from spiffworkflow_backend.models.service_account import ServiceAccountModel
from spiffworkflow_backend.models.task import TaskModel  # noqa: F401
from spiffworkflow_backend.models.user import SPIFF_GUEST_USER
from spiffworkflow_backend.models.user import SPIFF_NO_AUTH_USER
from spiffworkflow_backend.models.user import UserModel
from spiffworkflow_backend.services.authentication_service import AuthenticationService
from spiffworkflow_backend.services.authorization_service import AuthorizationService
from spiffworkflow_backend.services.user_service import UserService

"""
.. module:: crc.api.user
   :synopsis: Single Sign On (SSO) user login and session handlers
"""


def authentication_options() -> Response:
    response = AuthenticationService.authentication_options_for_api()
    return make_response(jsonify(response), 200)


# authorization_exclusion_list = ['status']
def verify_token(token: str | None = None, force_run: bool | None = False) -> None:
    """Verify the token for the user (if provided).

    If in production environment and token is not provided, gets user from the SSO headers and returns their token.

    Args:
        token: Optional[str]
        force_run: Optional[bool]

    Returns:
        token: str

    Raises:  # noqa: DAR401
        ApiError:  If not on production and token is not valid, returns an 'invalid_token' 403 error.
        If on production and user is not authenticated, returns a 'no_user' 403 error.
    """
    if not force_run and AuthorizationService.should_disable_auth_for_request():
        return None

    token_info = _find_token_from_headers(token)

    # This should never be set here but just in case
    _clear_auth_tokens_from_thread_local_data()

    user_model = None
    if token_info["token"] is not None:
        user_model = _get_user_model_from_token(token_info["token"])
    elif token_info["api_key"] is not None:
        user_model = _get_user_model_from_api_key(token_info["api_key"])

    if user_model:
        g.user = user_model

    # If the user is valid, store the token for this session
    if hasattr(g, "user") and g.user:
        # TODO: ensure we do not actually need g.token set and set g.authenticated instead.
        # I am pretty sure g.token is only actually used in UserService.has_user to
        # figure out if the if the user has logged in.
        if token_info["token"]:
            # This is an id token, so we don't have a refresh token yet
            g.token = token_info["token"]
            g.authenticated = True
            # we are getting the scope so it will decode the token and ensure it's valid.
            # this may be a better way to do this.
            get_scope(token_info["token"])
        elif token_info["api_key"]:
            g.authenticated = True
        return None

    raise ApiError(error_code="invalid_token", message="Cannot validate token.", status_code=401)


def login(
    authentication_identifier: str,
    redirect_url: str = "/",
    process_instance_id: int | None = None,
    task_guid: str | None = None,
) -> Response:
    frontend_url = str(current_app.config.get("SPIFFWORKFLOW_BACKEND_URL_FOR_FRONTEND"))
    if not redirect_url.startswith(frontend_url):
        raise InvalidRedirectUrlError(
            f"Invalid redirect url was given: '{redirect_url}'. It must match the domain the frontend is running on."
        )

    if current_app.config.get("SPIFFWORKFLOW_BACKEND_AUTHENTICATION_DISABLED"):
        AuthenticationService.create_guest_token(
            username=SPIFF_NO_AUTH_USER,
            group_identifier=SPIFF_NO_AUTH_GROUP,
            permission_target="/*",
            auth_token_properties={"authentication_disabled": True},
            authentication_identifier=authentication_identifier,
        )
        return redirect(redirect_url)

    if process_instance_id and task_guid and TaskModel.task_guid_allows_guest(task_guid, process_instance_id):
        AuthenticationService.create_guest_token(
            username=SPIFF_GUEST_USER,
            group_identifier=SPIFF_GUEST_GROUP,
            auth_token_properties={"only_guest_task_completion": True},
            authentication_identifier=authentication_identifier,
        )
        return redirect(redirect_url)

    state = AuthenticationService.generate_state(redirect_url, authentication_identifier)
    login_redirect_url = AuthenticationService().get_login_redirect_url(
        state.decode("UTF-8"), authentication_identifier=authentication_identifier
    )
    return redirect(login_redirect_url)


def login_return(code: str, state: str, session_state: str = "") -> Response | None:
    state_dict = ast.literal_eval(base64.b64decode(state).decode("utf-8"))
    state_redirect_url = state_dict["redirect_url"]
    authentication_identifier = state_dict["authentication_identifier"]
    auth_token_object = AuthenticationService().get_auth_token_object(code, authentication_identifier=authentication_identifier)
    if "id_token" in auth_token_object:
        id_token = auth_token_object["id_token"]
        user_info = _parse_id_token(id_token)

        if AuthenticationService.validate_id_or_access_token(id_token, authentication_identifier=authentication_identifier):
            if user_info and "error" not in user_info:
                user_model = AuthorizationService.create_user_from_sign_in(user_info)
                g.user = user_model.id
                g.token = auth_token_object["id_token"]
                if "refresh_token" in auth_token_object:
                    AuthenticationService.store_refresh_token(user_model.id, auth_token_object["refresh_token"])
                redirect_url = state_redirect_url
                tld = current_app.config["THREAD_LOCAL_DATA"]
                tld.new_access_token = auth_token_object["id_token"]
                tld.new_id_token = auth_token_object["id_token"]
                tld.new_authentication_identifier = authentication_identifier
                return redirect(redirect_url)

        # we normally clear cookies on 401, but there is a high chance you do not have any yet in this case
        raise ApiError(
            error_code="invalid_login",
            message="Login failed. Please try again",
            status_code=401,
        )

    else:
        # we normally clear cookies on 401, but there is a high chance you do not have any yet in this case
        current_app.logger.error(f"id_token not found in payload from provider: {auth_token_object}")
        raise ApiError(
            error_code="invalid_token",
            message="Login failed. Please try again",
            status_code=401,
        )


# FIXME: share more code with login_return and maybe attempt to get a refresh token
def login_with_access_token(access_token: str, authentication_identifier: str) -> Response:
    user_info = _parse_id_token(access_token)

    if AuthenticationService.validate_id_or_access_token(access_token, authentication_identifier=authentication_identifier):
        if user_info and "error" not in user_info:
            AuthorizationService.create_user_from_sign_in(user_info)
    else:
        raise ApiError(
            error_code="invalid_login",
            message="Login failed. Please try again",
            status_code=401,
        )

    return make_response(jsonify({"ok": True}))


def login_api(authentication_identifier: str) -> Response:
    redirect_url = "/v1.0/login_api_return"
    state = AuthenticationService.generate_state(redirect_url, authentication_identifier)
    login_redirect_url = AuthenticationService().get_login_redirect_url(state.decode("UTF-8"), redirect_url)
    return redirect(login_redirect_url)


def login_api_return(code: str, state: str, session_state: str) -> str:
    state_dict = ast.literal_eval(base64.b64decode(state).decode("utf-8"))
    state_dict["redirect_url"]

    auth_token_object = AuthenticationService().get_auth_token_object(code, "/v1.0/login_api_return")
    access_token: str = auth_token_object["access_token"]
    if access_token is None:
        raise MissingAccessTokenError("Cannot find the access token for the request")

    return access_token


def logout(id_token: str, authentication_identifier: str, redirect_url: str | None) -> Response:
    if redirect_url is None:
        redirect_url = ""
    AuthenticationService.set_user_has_logged_out()
    return AuthenticationService().logout(
        redirect_url=redirect_url, id_token=id_token, authentication_identifier=authentication_identifier
    )


def logout_return() -> Response:
    frontend_url = str(current_app.config["SPIFFWORKFLOW_BACKEND_URL_FOR_FRONTEND"])
    return redirect(f"{frontend_url}/")


def get_scope(token: str) -> str:
    scope = ""
    decoded_token = jwt.decode(token, options={"verify_signature": False})
    if "scope" in decoded_token:
        scope = decoded_token["scope"]
    return scope


# this isn't really a private method but it's also not a valid api call so underscoring it
def _set_new_access_token_in_cookie(
    response: flask.wrappers.Response,
) -> flask.wrappers.Response:
    """Checks if a new token has been set in THREAD_LOCAL_DATA and sets cookies if appropriate.

    It will also delete the cookies if the user has logged out.
    """
    tld = current_app.config["THREAD_LOCAL_DATA"]
    domain_for_frontend_cookie: str | None = re.sub(
        r"^https?:\/\/",
        "",
        current_app.config["SPIFFWORKFLOW_BACKEND_URL_FOR_FRONTEND"],
    )
    if domain_for_frontend_cookie and domain_for_frontend_cookie.startswith("localhost"):
        domain_for_frontend_cookie = None

    # fixme - we should not be passing the access token back to the client
    if hasattr(tld, "new_access_token") and tld.new_access_token:
        response.set_cookie("access_token", tld.new_access_token, domain=domain_for_frontend_cookie)

    # id_token is required for logging out since this gets passed back to the openid server
    if hasattr(tld, "new_id_token") and tld.new_id_token:
        response.set_cookie("id_token", tld.new_id_token, domain=domain_for_frontend_cookie)

    if hasattr(tld, "new_authentication_identifier") and tld.new_authentication_identifier:
        response.set_cookie("authentication_identifier", tld.new_authentication_identifier, domain=domain_for_frontend_cookie)

    if hasattr(tld, "user_has_logged_out") and tld.user_has_logged_out:
        response.set_cookie("id_token", "", max_age=0, domain=domain_for_frontend_cookie)
        response.set_cookie("access_token", "", max_age=0, domain=domain_for_frontend_cookie)
        response.set_cookie("authentication_identifier", "", max_age=0, domain=domain_for_frontend_cookie)

    _clear_auth_tokens_from_thread_local_data()

    return response


def _clear_auth_tokens_from_thread_local_data() -> None:
    tld = current_app.config["THREAD_LOCAL_DATA"]
    if hasattr(tld, "new_access_token"):
        delattr(tld, "new_access_token")
    if hasattr(tld, "new_id_token"):
        delattr(tld, "new_id_token")
    if hasattr(tld, "new_authentication_identifier"):
        delattr(tld, "new_authentication_identifier")
    if hasattr(tld, "user_has_logged_out"):
        delattr(tld, "user_has_logged_out")


def _force_logout_user_if_necessary(user_model: UserModel | None = None) -> bool:
    """Logs out a guest user if certain criteria gets met.

    * if the user is a no auth guest and we have auth enabled
    * if the user is a guest and goes somewhere else that does not allow guests
    """
    if user_model is not None:
        if (
            not current_app.config.get("SPIFFWORKFLOW_BACKEND_AUTHENTICATION_DISABLED")
            and user_model.username == SPIFF_NO_AUTH_USER
            and user_model.service_id == "spiff_guest_service_id"
        ) or (
            user_model.username == SPIFF_GUEST_USER
            and user_model.service_id == "spiff_guest_service_id"
            and not AuthorizationService.request_allows_guest_access()
        ):
            AuthenticationService.set_user_has_logged_out()
            return True
    return False


def _find_token_from_headers(token: str | None) -> dict[str, str | None]:
    api_key = None
    if not token and "Authorization" in request.headers:
        token = request.headers["Authorization"].removeprefix("Bearer ")

    if not token and "access_token" in request.cookies:
        if request.path.startswith(f"{V1_API_PATH_PREFIX}/process-data-file-download/") or request.path.startswith(
            f"{V1_API_PATH_PREFIX}/extensions-get-data/"
        ):
            token = request.cookies["access_token"]

    if not token and "SpiffWorkflow-Api-key" in request.headers:
        api_key = request.headers["SpiffWorkflow-Api-key"]

    token_info = {"token": token, "api_key": api_key}
    return token_info


def _get_user_model_from_api_key(api_key: str) -> UserModel | None:
    api_key_hash = ServiceAccountModel.hash_api_key(api_key)
    service_account = ServiceAccountModel.query.filter_by(api_key_hash=api_key_hash).first()
    user_model = None
    if service_account is not None:
        user_model = UserModel.query.filter_by(id=service_account.user_id).first()
    return user_model


def _get_user_model_from_token(token: str) -> UserModel | None:
    user_model = None
    decoded_token = _get_decoded_token(token)

    if decoded_token is not None:
        if "token_type" in decoded_token:
            token_type = decoded_token["token_type"]
            if token_type == "internal":  # noqa: S105
                try:
                    user_model = _get_user_from_decoded_internal_token(decoded_token)
                except Exception as e:
                    current_app.logger.error(f"Exception in verify_token getting user from decoded internal token. {e}")

            # if the user is forced logged out then stop processing the token
            if _force_logout_user_if_necessary(user_model):
                return None

        elif "iss" in decoded_token.keys():
            user_info = None
            authentication_identifier = _get_authentication_identifier_from_request()
            try:
                if AuthenticationService.validate_id_or_access_token(token, authentication_identifier=authentication_identifier):
                    user_info = decoded_token
            except TokenExpiredError as token_expired_error:
                # Try to refresh the token
                user = UserService.get_user_by_service_and_service_id(decoded_token["iss"], decoded_token["sub"])
                if user:
                    refresh_token = AuthenticationService.get_refresh_token(user.id)
                    if refresh_token:
                        auth_token: dict = AuthenticationService.get_auth_token_from_refresh_token(
                            refresh_token, authentication_identifier=authentication_identifier
                        )
                        if auth_token and "error" not in auth_token and "id_token" in auth_token:
                            tld = current_app.config["THREAD_LOCAL_DATA"]
                            tld.new_access_token = auth_token["id_token"]
                            tld.new_id_token = auth_token["id_token"]
                            # We have the user, but this code is a bit convoluted, and will later demand
                            # a user_info object so it can look up the user.  Sorry to leave this crap here.
                            user_info = {
                                "sub": user.service_id,
                                "iss": user.service,
                            }

                if user_info is None:
                    AuthenticationService.set_user_has_logged_out()
                    raise ApiError(
                        error_code="invalid_token",
                        message="Your token is expired. Please Login",
                        status_code=401,
                    ) from token_expired_error

            except Exception as e:
                AuthenticationService.set_user_has_logged_out()
                raise ApiError(
                    error_code="fail_get_user_info",
                    message="Cannot get user info from token",
                    status_code=401,
                ) from e
            if user_info is not None and "error" not in user_info and "iss" in user_info:  # not sure what to test yet
                user_model = (
                    UserModel.query.filter(UserModel.service == user_info["iss"])
                    .filter(UserModel.service_id == user_info["sub"])
                    .first()
                )
                if user_model is None:
                    AuthenticationService.set_user_has_logged_out()
                    raise ApiError(
                        error_code="invalid_user",
                        message="Invalid user. Please log in.",
                        status_code=401,
                    )
            # no user_info
            else:
                AuthenticationService.set_user_has_logged_out()
                raise ApiError(
                    error_code="no_user_info",
                    message="Cannot retrieve user info",
                    status_code=401,
                )

        else:
            current_app.logger.debug("token_type not in decode_token in verify_token")
            AuthenticationService.set_user_has_logged_out()
            raise ApiError(
                error_code="invalid_token",
                message="Invalid token. Please log in.",
                status_code=401,
            )

    return user_model


def _get_user_from_decoded_internal_token(decoded_token: dict) -> UserModel | None:
    sub = decoded_token["sub"]
    parts = sub.split("::")
    service = parts[0].split(":")[1]
    service_id = parts[1].split(":")[1]
    user: UserModel = UserModel.query.filter(UserModel.service == service).filter(UserModel.service_id == service_id).first()
    if user:
        return user
    user = UserService.create_user(service_id, service, service_id)
    return user


def _get_decoded_token(token: str) -> dict | None:
    try:
        decoded_token: dict = jwt.decode(token, options={"verify_signature": False})
    except Exception as e:
        raise ApiError(error_code="invalid_token", message="Cannot decode token.") from e
    else:
        if "token_type" in decoded_token or "iss" in decoded_token:
            return decoded_token
        else:
            current_app.logger.error(f"Unknown token type in get_decoded_token: token: {token}")
            raise ApiError(
                error_code="unknown_token",
                message="Unknown token type in get_decoded_token",
            )


def _parse_id_token(token: str) -> Any:
    """Parse the id token."""
    parts = token.split(".")
    if len(parts) != 3:
        raise Exception("Incorrect id token format")

    payload = parts[1]
    padded = payload + "=" * (4 - len(payload) % 4)
    decoded = base64.b64decode(padded)
    return json.loads(decoded)


def _get_authentication_identifier_from_request() -> str:
    if "authentication_identifier" in request.cookies:
        return request.cookies["authentication_identifier"]
    if "SpiffWorkflow-Authentication-Identifier" in request.headers:
        authentication_identifier: str = request.headers["SpiffWorkflow-Authentication-Identifier"]
        return authentication_identifier
    return "default"
