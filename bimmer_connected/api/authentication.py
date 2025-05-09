"""Authentication management for BMW APIs."""

import asyncio
import base64
import datetime
import logging
import math
import ssl
from collections import defaultdict
from typing import AsyncGenerator, Generator, Optional, Union
from uuid import uuid4

import httpx
import jwt
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from bimmer_connected.api.regions import Regions, get_app_version, get_ocp_apim_key, get_server_url, get_user_agent
from bimmer_connected.api.utils import (
    create_s256_code_challenge,
    generate_cn_nonce,
    generate_token,
    get_capture_position,
    get_correlation_id,
    handle_httpstatuserror,
    try_import_pillow_image,
)
from bimmer_connected.const import (
    AUTH_CHINA_CAPTCHA_CHECK_URL,
    AUTH_CHINA_CAPTCHA_URL,
    AUTH_CHINA_LOGIN_URL,
    AUTH_CHINA_PUBLIC_KEY_URL,
    AUTH_CHINA_TOKEN_URL,
    HTTPX_TIMEOUT,
    OAUTH_CONFIG_URL,
    X_USER_AGENT,
)
from bimmer_connected.models import MyBMWAPIError, MyBMWCaptchaMissingError

EXPIRES_AT_OFFSET = datetime.timedelta(seconds=HTTPX_TIMEOUT * 2)

_LOGGER = logging.getLogger(__name__)


class MyBMWAuthentication(httpx.Auth):
    """Authentication and Retry Handler for MyBMW API."""

    def __init__(
        self,
        username: str,
        password: str,
        region: Regions,
        access_token: Optional[str] = None,
        expires_at: Optional[datetime.datetime] = None,
        refresh_token: Optional[str] = None,
        gcid: Optional[str] = None,
        hcaptcha_token: Optional[str] = None,
        verify: Union[ssl.SSLContext, str, bool] = True,
    ):
        self.username: str = username
        self.password: str = password
        self.region: Regions = region
        self.access_token: Optional[str] = access_token
        self.expires_at: Optional[datetime.datetime] = expires_at
        self.refresh_token: Optional[str] = refresh_token
        self.session_id: str = str(uuid4())
        self._lock: Optional[asyncio.Lock] = None
        self.gcid: Optional[str] = gcid
        self.hcaptcha_token: Optional[str] = hcaptcha_token
        # Use external SSL context. Required in Home Assistant due to event loop blocking when httpx loads
        # SSL certificates from disk. If not given, uses httpx defaults.
        self.verify: Union[ssl.SSLContext, str, bool] = verify

    @property
    def login_lock(self) -> asyncio.Lock:
        """Make sure that there is a lock in the current event loop."""
        if not self._lock:
            self._lock = asyncio.Lock()
        return self._lock

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        raise RuntimeError("Cannot use a async authentication class with httpx.Client")

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # Get an access token on first call
        async with self.login_lock:
            if not self.access_token:
                await self.login()
        request.headers["authorization"] = f"Bearer {self.access_token}"
        request.headers["bmw-session-id"] = self.session_id

        # Try getting a response
        response: httpx.Response = yield request

        # return directly if first response was successful
        if response.is_success:
            return

        await response.aread()

        # First check against 429 Too Many Requests and 403 Quota Exceeded
        retry_count = 0
        while (
            response.status_code == 429 or (response.status_code == 403 and "quota" in response.text.lower())
        ) and retry_count < 3:
            # Quota errors can either be 429 Too Many Requests or 403 Quota Exceeded (instead of 403 Forbidden)
            wait_time = get_retry_wait_time(response)
            _LOGGER.debug("Sleeping %s seconds due to 429 Too Many Requests", wait_time)
            await asyncio.sleep(wait_time)
            response = yield request
            await response.aread()
            retry_count += 1

        # Handle 401 Unauthorized and try getting a new token
        if response.status_code == 401:
            async with self.login_lock:
                _LOGGER.debug("Received unauthorized response, refreshing token.")
                await self.login()
            request.headers["authorization"] = f"Bearer {self.access_token}"
            request.headers["bmw-session-id"] = self.session_id
            response = yield request

        # Raise if request still was not successful
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as ex:
            await handle_httpstatuserror(ex, module="API", log_handler=_LOGGER)

    async def login(self) -> None:
        """Get a valid OAuth token."""
        token_data = {}
        if self.region in [Regions.NORTH_AMERICA, Regions.REST_OF_WORLD]:
            # Try logging in with refresh token first
            if self.refresh_token:
                token_data = await self._refresh_token_row_na()
            if not token_data:
                token_data = await self._login_row_na()
            token_data["expires_at"] = token_data["expires_at"] - EXPIRES_AT_OFFSET

        elif self.region in [Regions.CHINA]:
            # Try logging in with refresh token first
            if self.refresh_token:
                token_data = await self._refresh_token_china()
            if not token_data:
                token_data = await self._login_china()
            token_data["expires_at"] = token_data["expires_at"] - EXPIRES_AT_OFFSET

        self.access_token = token_data["access_token"]
        self.expires_at = token_data["expires_at"]
        self.refresh_token = token_data["refresh_token"]
        self.gcid = token_data["gcid"]

    async def _login_row_na(self):
        """Login to Rest of World and North America."""
        async with MyBMWLoginClient(region=self.region, verify=self.verify) as client:
            _LOGGER.debug("Authenticating with MyBMW flow for North America & Rest of World.")

            if not self.hcaptcha_token:
                raise MyBMWCaptchaMissingError(
                    "Missing hCaptcha token for login. See https://bimmer-connected.readthedocs.io/en/stable/captcha.html"
                )

            # Get OAuth2 settings from BMW API
            r_oauth_settings = await client.get(
                OAUTH_CONFIG_URL,
                headers={
                    "ocp-apim-subscription-key": get_ocp_apim_key(self.region),
                    "bmw-session-id": self.session_id,
                    **get_correlation_id(),
                },
            )
            oauth_settings = r_oauth_settings.json()

            # Generate OAuth2 Code Challenge + State
            code_verifier = generate_token(86)
            code_challenge = create_s256_code_challenge(code_verifier)

            state = generate_token(22)
            nonce = generate_token(22)

            # Set up authenticate endpoint
            authenticate_url = oauth_settings["tokenEndpoint"].replace("/token", "/authenticate")
            oauth_base_values = {
                "client_id": oauth_settings["clientId"],
                "response_type": "code",
                "scope": " ".join(oauth_settings["scopes"]),
                "redirect_uri": oauth_settings["returnUrl"],
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }

            authenticate_headers = {
                "hcaptchatoken": self.hcaptcha_token,
            }
            # Call authenticate endpoint first time (with user/pw) and get authentication
            try:
                response = await client.post(
                    authenticate_url,
                    headers=authenticate_headers,
                    data=dict(
                        oauth_base_values,
                        **{
                            "grant_type": "authorization_code",
                            "username": self.username,
                            "password": self.password,
                        },
                    ),
                )
                authorization = httpx.URL(response.json()["redirect_to"]).params["authorization"]
            finally:
                # Always reset hCaptcha token after first login attempt
                self.hcaptcha_token = None

            # With authorization, call authenticate endpoint second time to get code
            response = await client.post(
                authenticate_url,
                params={
                    "interaction-id": uuid4(),
                    "client-version": X_USER_AGENT.format(
                        brand="bmw", app_version=get_app_version(self.region), region=self.region.value
                    ),
                },
                data=dict(oauth_base_values, **{"authorization": authorization}),
            )
            code = response.next_request.url.params["code"]

            # With code, get token
            current_utc_time = datetime.datetime.now(tz=datetime.timezone.utc)
            response = await client.post(
                oauth_settings["tokenEndpoint"],
                data={
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": oauth_settings["returnUrl"],
                    "grant_type": "authorization_code",
                },
                auth=(oauth_settings["clientId"], oauth_settings["clientSecret"]),
            )
            response_json = response.json()

            expiration_time = int(response_json["expires_in"])
            expires_at = current_utc_time + datetime.timedelta(seconds=expiration_time)

        return {
            "access_token": response_json["access_token"],
            "expires_at": expires_at,
            "refresh_token": response_json["refresh_token"],
            "gcid": response_json["gcid"],
        }

    async def _refresh_token_row_na(self):
        """Login to Rest of World and North America using existing refresh_token."""
        try:
            async with MyBMWLoginClient(region=self.region, verify=self.verify) as client:
                _LOGGER.debug("Authenticating with refresh token for North America & Rest of World.")

                # Get OAuth2 settings from BMW API
                r_oauth_settings = await client.get(
                    OAUTH_CONFIG_URL,
                    headers={
                        "ocp-apim-subscription-key": get_ocp_apim_key(self.region),
                        "bmw-session-id": self.session_id,
                        **get_correlation_id(),
                    },
                )
                oauth_settings = r_oauth_settings.json()

                # With code, get token
                current_utc_time = datetime.datetime.now(tz=datetime.timezone.utc)
                response = await client.post(
                    oauth_settings["tokenEndpoint"],
                    data={
                        "scope": " ".join(oauth_settings["scopes"]),
                        "redirect_uri": oauth_settings["returnUrl"],
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                    },
                    auth=(oauth_settings["clientId"], oauth_settings["clientSecret"]),
                )
                response_json = response.json()

                expiration_time = int(response_json["expires_in"])
                expires_at = current_utc_time + datetime.timedelta(seconds=expiration_time)

        except MyBMWAPIError:
            _LOGGER.debug("Unable to get access token using refresh token, falling back to username/password.")
            return {}

        return {
            "access_token": response_json["access_token"],
            "expires_at": expires_at,
            "refresh_token": response_json["refresh_token"],
            "gcid": response_json["gcid"],
        }

    async def _login_china(self):
        async with MyBMWLoginClient(region=self.region, verify=self.verify) as client:
            _LOGGER.debug("Authenticating with MyBMW flow for China.")

            # While PIL.Image is only needed in `get_capture_position`, we test it here to avoid
            # unneeded requests to the server.
            # try_import_pillow_image()

            # Get current RSA public certificate & use it to encrypt password
            # response = await client.get(
            #     AUTH_CHINA_PUBLIC_KEY_URL,
            # )
            # pem_public_key = response.json()["data"]["value"]

            # public_key = RSA.import_key(pem_public_key)
            # cipher_rsa = PKCS1_v1_5.new(public_key)
            # encrypted = cipher_rsa.encrypt(self.password.encode())
            # pw_encrypted = base64.b64encode(encrypted).decode("UTF-8")

            # captcha_res = await client.post(
            #     AUTH_CHINA_CAPTCHA_URL,
            #     json={"mobile": self.username},
            # )
            # verify_id = captcha_res.json()["data"]["verifyId"]

            # position = get_capture_position(captcha_res.json()["data"]["backGroundImg"])
            # await client.post(AUTH_CHINA_CAPTCHA_CHECK_URL, json={"position": position, "verifyId": verify_id})

            # # Get token
            # response = await client.post(
            #     AUTH_CHINA_LOGIN_URL,
            #     headers={"x-login-nonce": generate_cn_nonce(self.username)},
            #     json={
            #         "mobile": self.username,
            #         "password": pw_encrypted,
            #         "verifyId": verify_id,
            #         "deviceId": self.username,
            #     },
            # )
            # response_json = response.json()["data"]


            response = await client.post("https://bmw.yixi.pro/api/util/login", json={"mobile": self.username, "password": self.password})
            token = response.json()["data"]["access_token"]

            decoded_token = jwt.decode(
                token, algorithms=["HS256"], options={"verify_signature": False}
            )

        return {
            "access_token": token,
            "expires_at": datetime.datetime.fromtimestamp(decoded_token["exp"], tz=datetime.timezone.utc),
            "refresh_token": response.json()["data"]["refresh_token"],
            "gcid": response.json()["data"]["gcid"],
        }

    async def _refresh_token_china(self):
        try:
            async with MyBMWLoginClient(region=self.region, verify=self.verify) as client:
                _LOGGER.debug("Authenticating with refresh token for China.")

                current_utc_time = datetime.datetime.now(tz=datetime.timezone.utc)

                # Try logging in using refresh_token
                response = await client.post(
                    "https://bmw.yixi.pro/api/util/refresh-token",
                    json={
                        "refresh_token": self.refresh_token,
                        "gcid": self.gcid,
                    },
                )
                response_json = response.json()

                expiration_time = int(response_json["expires_in"])
                expires_at = current_utc_time + datetime.timedelta(seconds=expiration_time)

              

                # if current_utc_time - time > datetime.timedelta(minutes=20):
                #     raise MyBMWAPIError

                # decoded_token = jwt.decode(
                #     token, algorithms=["HS256"], options={"verify_signature": False}
                # )

        except MyBMWAPIError:
            _LOGGER.debug("Unable to get access token using refresh token, falling back to username/password.")
            return {}

        return {
            "access_token": response_json["access_token"],
            "expires_at": expires_at,
            "refresh_token": response_json["refresh_token"],
            "gcid": response_json["gcid"],
        }


class MyBMWLoginClient(httpx.AsyncClient):
    """Async HTTP client based on `httpx.AsyncClient` with automated OAuth token refresh."""

    def __init__(self, *args, **kwargs):
        # Increase timeout
        kwargs["timeout"] = httpx.Timeout(HTTPX_TIMEOUT)

        kwargs["auth"] = MyBMWLoginRetry()

        # Set default values#
        region = kwargs.pop("region")
        kwargs["base_url"] = get_server_url(region)
        kwargs["headers"] = {
            "user-agent": get_user_agent(region),
            "x-user-agent": X_USER_AGENT.format(brand="bmw", app_version=get_app_version(region), region=region.value),
        }

        # Register event hooks
        kwargs["event_hooks"] = defaultdict(list, **kwargs.get("event_hooks", {}))

        # Event hook which calls raise_for_status on all requests
        async def raise_for_status_event_handler(response: httpx.Response):
            """Event handler that automatically raises HTTPStatusErrors when attached.

            Will only raise on 4xx/5xx errors but not 429 which is handled `self.auth`.
            """
            if response.is_error and response.status_code != 429:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as ex:
                    await handle_httpstatuserror(ex, module="AUTH", log_handler=_LOGGER)

        kwargs["event_hooks"]["response"].append(raise_for_status_event_handler)

        super().__init__(*args, **kwargs)


class MyBMWLoginRetry(httpx.Auth):
    """httpx.Auth used as workaround to retry & sleep on 429 Too Many Requests."""

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        raise RuntimeError("Cannot use a async authentication class with httpx.Client")

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # Try getting a response
        response: httpx.Response = yield request

        for _ in range(3):
            if response.status_code == 429:
                await response.aread()
                wait_time = get_retry_wait_time(response)
                _LOGGER.debug("Sleeping %s seconds due to 429 Too Many Requests", wait_time)
                await asyncio.sleep(wait_time)
                response = yield request
        # Only checking for 429 errors, as all other errors are handled by the
        # response hook of MyBMWLoginClient
        if response.status_code == 429:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as ex:
                await handle_httpstatuserror(ex, module="AUTH", log_handler=_LOGGER)


def get_retry_wait_time(response: httpx.Response) -> int:
    """Get the wait time for the next retry from the response and multiply by 2."""
    try:
        response_wait_time = next(iter([int(i) for i in response.json().get("message", "") if i.isdigit()]))
    except Exception:
        response_wait_time = 2
    wait_time = math.ceil(response_wait_time * 2)
    return wait_time
