import json
import logging
from base64 import b64decode
from unittest import mock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

from snowflake.connector.auth import AuthByWorkloadIdentity
from snowflake.connector.errors import ProgrammingError
from snowflake.connector.vendored.requests.exceptions import (
    ConnectTimeout,
    HTTPError,
    Timeout,
)
from snowflake.connector.wif_util import (
    AZURE_ISSUER_PREFIXES,
    AttestationProvider,
    get_aws_partition,
    get_aws_sts_hostname,
)

from ..csp_helpers import FakeAwsEnvironment, FakeGceMetadataService, gen_dummy_id_token

logger = logging.getLogger(__name__)


def extract_api_data(auth_class: AuthByWorkloadIdentity):
    """Extracts the 'data' portion of the request body populated by the given auth class."""
    req_body = {"data": {}}
    auth_class.update_body(req_body)
    return req_body["data"]


def verify_aws_token(token: str, region: str):
    """Performs some basic checks on a 'token' produced for AWS, to ensure it includes the expected fields."""
    decoded_token = json.loads(b64decode(token))

    parsed_url = urlparse(decoded_token["url"])
    assert parsed_url.scheme == "https"
    assert parsed_url.hostname == f"sts.{region}.amazonaws.com"
    query_string = parse_qs(parsed_url.query)
    assert query_string.get("Action")[0] == "GetCallerIdentity"
    assert query_string.get("Version")[0] == "2011-06-15"

    assert decoded_token["method"] == "POST"

    headers = decoded_token["headers"]
    assert set(headers.keys()) == {
        "Host",
        "X-Snowflake-Audience",
        "X-Amz-Date",
        "X-Amz-Security-Token",
        "Authorization",
    }
    assert headers["Host"] == f"sts.{region}.amazonaws.com"
    assert headers["X-Snowflake-Audience"] == "snowflakecomputing.com"


# -- OIDC Tests --


def test_explicit_oidc_valid_inline_token_plumbed_to_api():
    dummy_token = gen_dummy_id_token(sub="service-1", iss="issuer-1")
    auth_class = AuthByWorkloadIdentity(
        provider=AttestationProvider.OIDC, token=dummy_token
    )
    auth_class.prepare()

    assert extract_api_data(auth_class) == {
        "AUTHENTICATOR": "WORKLOAD_IDENTITY",
        "PROVIDER": "OIDC",
        "TOKEN": dummy_token,
    }


def test_explicit_oidc_valid_inline_token_generates_unique_assertion_content():
    dummy_token = gen_dummy_id_token(sub="service-1", iss="issuer-1")
    auth_class = AuthByWorkloadIdentity(
        provider=AttestationProvider.OIDC, token=dummy_token
    )
    auth_class.prepare()
    assert (
        auth_class.assertion_content
        == '{"_provider":"OIDC","iss":"issuer-1","sub":"service-1"}'
    )


def test_explicit_oidc_invalid_inline_token_raises_error():
    invalid_token = "not-a-jwt"
    auth_class = AuthByWorkloadIdentity(
        provider=AttestationProvider.OIDC, token=invalid_token
    )
    with pytest.raises(ProgrammingError) as excinfo:
        auth_class.prepare()
    assert "No workload identity credential was found for 'OIDC'" in str(excinfo.value)


def test_explicit_oidc_no_token_raises_error():
    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.OIDC, token=None)
    with pytest.raises(ProgrammingError) as excinfo:
        auth_class.prepare()
    assert "No workload identity credential was found for 'OIDC'" in str(excinfo.value)


# -- AWS Tests --


def test_explicit_aws_no_auth_raises_error(fake_aws_environment: FakeAwsEnvironment):
    fake_aws_environment.credentials = None

    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AWS)
    with pytest.raises(ProgrammingError) as excinfo:
        auth_class.prepare()
    assert "No workload identity credential was found for 'AWS'" in str(excinfo.value)


def test_explicit_aws_encodes_audience_host_signature_to_api(
    fake_aws_environment: FakeAwsEnvironment,
):
    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AWS)
    auth_class.prepare()

    data = extract_api_data(auth_class)
    assert data["AUTHENTICATOR"] == "WORKLOAD_IDENTITY"
    assert data["PROVIDER"] == "AWS"
    verify_aws_token(data["TOKEN"], fake_aws_environment.region)


def test_explicit_aws_uses_regional_hostname(fake_aws_environment: FakeAwsEnvironment):
    fake_aws_environment.region = "antarctica-northeast-3"

    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AWS)
    auth_class.prepare()

    data = extract_api_data(auth_class)
    decoded_token = json.loads(b64decode(data["TOKEN"]))
    hostname_from_url = urlparse(decoded_token["url"]).hostname
    hostname_from_header = decoded_token["headers"]["Host"]

    expected_hostname = "sts.antarctica-northeast-3.amazonaws.com"
    assert expected_hostname == hostname_from_url
    assert expected_hostname == hostname_from_header


def test_explicit_aws_generates_unique_assertion_content(
    fake_aws_environment: FakeAwsEnvironment,
):
    fake_aws_environment.arn = (
        "arn:aws:sts::123456789:assumed-role/A-Different-Role/i-34afe100cad287fab"
    )
    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AWS)
    auth_class.prepare()

    assert (
        '{"_provider":"AWS","arn":"arn:aws:sts::123456789:assumed-role/A-Different-Role/i-34afe100cad287fab"}'
        == auth_class.assertion_content
    )


@pytest.mark.parametrize(
    "arn, expected_partition",
    [
        ("arn:aws:iam::123456789012:role/MyTestRole", "aws"),
        (
            "arn:aws-cn:ec2:cn-north-1:987654321098:instance/i-1234567890abcdef0",
            "aws-cn",
        ),
        ("arn:aws-us-gov:s3:::my-gov-bucket", "aws-us-gov"),
        ("arn:aws:s3:::my-bucket/my/key", "aws"),
        ("arn:aws:lambda:us-east-1:123456789012:function:my-function", "aws"),
        ("arn:aws:sns:eu-west-1:111122223333:my-topic", "aws"),
        # Edge cases / Invalid inputs
        ("invalid-arn", None),
        ("arn::service:region:account:resource", None),  # Missing partition
        ("arn:aws:iam:", "aws"),  # Incomplete ARN, but partition is present
        ("", None),  # Empty string
        (None, None),  # None input
        (123, None),  # Non-string input
    ],
)
def test_get_aws_partition_valid_and_invalid_arns(arn, expected_partition):
    assert get_aws_partition(arn) == expected_partition


@pytest.mark.parametrize(
    "region, partition, expected_hostname",
    [
        # AWS partition
        ("us-east-1", "aws", "sts.us-east-1.amazonaws.com"),
        ("eu-west-2", "aws", "sts.eu-west-2.amazonaws.com"),
        ("ap-southeast-1", "aws", "sts.ap-southeast-1.amazonaws.com"),
        (
            "us-east-1",
            "aws",
            "sts.us-east-1.amazonaws.com",
        ),  # Redundant but good for coverage
        # AWS China partition
        ("cn-north-1", "aws-cn", "sts.cn-north-1.amazonaws.com.cn"),
        ("cn-northwest-1", "aws-cn", "sts.cn-northwest-1.amazonaws.com.cn"),
        ("", "aws-cn", None),  # No global endpoint for 'aws-cn' without region
        # AWS GovCloud partition
        ("us-gov-west-1", "aws-us-gov", "sts.us-gov-west-1.amazonaws.com"),
        ("us-gov-east-1", "aws-us-gov", "sts.us-gov-east-1.amazonaws.com"),
        ("", "aws-us-gov", None),  # No global endpoint for 'aws-us-gov' without region
        # Invalid/Edge cases
        ("us-east-1", "unknown-partition", None),  # Unknown partition
        ("some-region", "invalid-partition", None),  # Invalid partition
        (None, "aws", None),  # None region
        ("us-east-1", None, None),  # None partition
        (123, "aws", None),  # Non-string region
        ("us-east-1", 456, None),  # Non-string partition
        ("", "", None),  # Empty region and partition
        ("us-east-1", "", None),  # Empty partition
        (
            "invalid-region",
            "aws",
            "sts.invalid-region.amazonaws.com",
        ),  # Valid format, invalid region name
    ],
)
def test_get_aws_sts_hostname_valid_and_invalid_inputs(
    region, partition, expected_hostname
):
    assert get_aws_sts_hostname(region, partition) == expected_hostname


# -- GCP Tests --


@pytest.mark.parametrize(
    "exception",
    [
        HTTPError(),
        Timeout(),
        ConnectTimeout(),
    ],
)
def test_explicit_gcp_metadata_server_error_raises_auth_error(exception):
    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.GCP)
    with mock.patch(
        "snowflake.connector.vendored.requests.request", side_effect=exception
    ):
        with pytest.raises(ProgrammingError) as excinfo:
            auth_class.prepare()
        assert "No workload identity credential was found for 'GCP'" in str(
            excinfo.value
        )


def test_explicit_gcp_wrong_issuer_raises_error(
    fake_gce_metadata_service: FakeGceMetadataService,
):
    fake_gce_metadata_service.iss = "not-google"

    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.GCP)
    with pytest.raises(ProgrammingError) as excinfo:
        auth_class.prepare()
    assert "No workload identity credential was found for 'GCP'" in str(excinfo.value)


def test_explicit_gcp_plumbs_token_to_api(
    fake_gce_metadata_service: FakeGceMetadataService,
):
    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.GCP)
    auth_class.prepare()

    assert extract_api_data(auth_class) == {
        "AUTHENTICATOR": "WORKLOAD_IDENTITY",
        "PROVIDER": "GCP",
        "TOKEN": fake_gce_metadata_service.token,
    }


def test_explicit_gcp_generates_unique_assertion_content(
    fake_gce_metadata_service: FakeGceMetadataService,
):
    fake_gce_metadata_service.sub = "123456"

    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.GCP)
    auth_class.prepare()

    assert auth_class.assertion_content == '{"_provider":"GCP","sub":"123456"}'


# -- Azure Tests --


@pytest.mark.parametrize(
    "exception",
    [
        HTTPError(),
        Timeout(),
        ConnectTimeout(),
    ],
)
def test_explicit_azure_metadata_server_error_raises_auth_error(exception):
    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AZURE)
    with mock.patch(
        "snowflake.connector.vendored.requests.request", side_effect=exception
    ):
        with pytest.raises(ProgrammingError) as excinfo:
            auth_class.prepare()
        assert "No workload identity credential was found for 'AZURE'" in str(
            excinfo.value
        )


def test_explicit_azure_wrong_issuer_raises_error(fake_azure_metadata_service):
    fake_azure_metadata_service.iss = "https://notazure.com"

    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AZURE)
    with pytest.raises(ProgrammingError) as excinfo:
        auth_class.prepare()
    assert "No workload identity credential was found for 'AZURE'" in str(excinfo.value)


@pytest.mark.parametrize(
    "issuer",
    [
        "https://sts.windows.net/067802cd-8f92-4c7c-bceb-ea8f15d31cc5",
        "https://login.microsoftonline.com/067802cd-8f92-4c7c-bceb-ea8f15d31cc5",
        "https://login.microsoftonline.com/067802cd-8f92-4c7c-bceb-ea8f15d31cc5/v2.0",
    ],
    ids=["v1", "v2_without_suffix", "v2_with_suffix"],
)
def test_explicit_azure_v1_and_v2_issuers_accepted(fake_azure_metadata_service, issuer):
    fake_azure_metadata_service.iss = issuer

    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AZURE)
    auth_class.prepare()

    assert issuer == json.loads(auth_class.assertion_content)["iss"]


def test_explicit_azure_plumbs_token_to_api(fake_azure_metadata_service):
    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AZURE)
    auth_class.prepare()

    assert extract_api_data(auth_class) == {
        "AUTHENTICATOR": "WORKLOAD_IDENTITY",
        "PROVIDER": "AZURE",
        "TOKEN": fake_azure_metadata_service.token,
    }


def test_explicit_azure_generates_unique_assertion_content(fake_azure_metadata_service):
    fake_azure_metadata_service.iss = (
        "https://sts.windows.net/2c0183ed-cf17-480d-b3f7-df91bc0a97cd"
    )
    fake_azure_metadata_service.sub = "611ab25b-2e81-4e18-92a7-b21f2bebb269"

    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AZURE)
    auth_class.prepare()

    assert (
        '{"_provider":"AZURE","iss":"https://sts.windows.net/2c0183ed-cf17-480d-b3f7-df91bc0a97cd","sub":"611ab25b-2e81-4e18-92a7-b21f2bebb269"}'
        == auth_class.assertion_content
    )


def test_explicit_azure_uses_default_entra_resource_if_unspecified(
    fake_azure_metadata_service,
):
    auth_class = AuthByWorkloadIdentity(provider=AttestationProvider.AZURE)
    auth_class.prepare()

    token = fake_azure_metadata_service.token
    parsed = jwt.decode(token, options={"verify_signature": False})
    assert (
        parsed["aud"] == "api://fd3f753b-eed3-462c-b6a7-a4b5bb650aad"
    )  # the default entra resource defined in wif_util.py.


def test_explicit_azure_uses_explicit_entra_resource(fake_azure_metadata_service):
    auth_class = AuthByWorkloadIdentity(
        provider=AttestationProvider.AZURE, entra_resource="api://non-standard"
    )
    auth_class.prepare()

    token = fake_azure_metadata_service.token
    parsed = jwt.decode(token, options={"verify_signature": False})
    assert parsed["aud"] == "api://non-standard"


@pytest.mark.parametrize(
    "issuer",
    [
        "https://sts.windows.net/067802cd-8f92-4c7c-bceb-ea8f15d31cc5",
        "https://sts.chinacloudapi.cn/067802cd-8f92-4c7c-bceb-ea8f15d31cc5",
        "https://login.microsoftonline.com/067802cd-8f92-4c7c-bceb-ea8f15d31cc5/v2.0",
        "https://login.microsoftonline.us/067802cd-8f92-4c7c-bceb-ea8f15d31cc5/v2.0",
        "https://login.partner.microsoftonline.cn/067802cd-8f92-4c7c-bceb-ea8f15d31cc5/v2.0",
    ],
)
def test_azure_issuer_prefixes(issuer):
    assert any(
        issuer.startswith(issuer_prefix) for issuer_prefix in AZURE_ISSUER_PREFIXES
    )


# -- Auto-detect Tests --


def test_autodetect_aws_present(
    no_metadata_service, fake_aws_environment: FakeAwsEnvironment
):
    auth_class = AuthByWorkloadIdentity(provider=None)
    auth_class.prepare()

    data = extract_api_data(auth_class)
    assert data["AUTHENTICATOR"] == "WORKLOAD_IDENTITY"
    assert data["PROVIDER"] == "AWS"
    verify_aws_token(data["TOKEN"], fake_aws_environment.region)


def test_autodetect_gcp_present(fake_gce_metadata_service: FakeGceMetadataService):
    auth_class = AuthByWorkloadIdentity(provider=None)
    auth_class.prepare()

    assert extract_api_data(auth_class) == {
        "AUTHENTICATOR": "WORKLOAD_IDENTITY",
        "PROVIDER": "GCP",
        "TOKEN": fake_gce_metadata_service.token,
    }


def test_autodetect_azure_present(fake_azure_metadata_service):
    auth_class = AuthByWorkloadIdentity(provider=None)
    auth_class.prepare()

    assert extract_api_data(auth_class) == {
        "AUTHENTICATOR": "WORKLOAD_IDENTITY",
        "PROVIDER": "AZURE",
        "TOKEN": fake_azure_metadata_service.token,
    }


def test_autodetect_oidc_present(no_metadata_service):
    dummy_token = gen_dummy_id_token(sub="service-1", iss="issuer-1")
    auth_class = AuthByWorkloadIdentity(provider=None, token=dummy_token)
    auth_class.prepare()

    assert extract_api_data(auth_class) == {
        "AUTHENTICATOR": "WORKLOAD_IDENTITY",
        "PROVIDER": "OIDC",
        "TOKEN": dummy_token,
    }


def test_autodetect_no_provider_raises_error(no_metadata_service):
    auth_class = AuthByWorkloadIdentity(provider=None, token=None)
    with pytest.raises(ProgrammingError) as excinfo:
        auth_class.prepare()
    assert "No workload identity credential was found for 'auto-detect" in str(
        excinfo.value
    )
