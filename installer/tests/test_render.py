"""Tests for installer/render.py and the rendered CloudFormation template."""

from pathlib import Path

import pytest
import yaml

from installer.render import render

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "installer.yaml"
HANDLER = ROOT / "function" / "handler.py"


class _CfnLoader(yaml.SafeLoader):
    pass


def _any_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node, deep=True)}
    return {suffix: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _any_tag)


@pytest.fixture(scope="module")
def rendered() -> str:
    return render(TEMPLATE, HANDLER)


@pytest.fixture(scope="module")
def doc(rendered):
    return yaml.load(rendered, Loader=_CfnLoader)


def test_render_inlines_handler_verbatim(doc):
    zipfile = doc["Resources"]["InstallerFunction"]["Properties"]["Code"]["ZipFile"]
    assert zipfile == HANDLER.read_text()


def test_render_under_template_body_limit(rendered):
    assert len(rendered.encode()) < 51200


@pytest.mark.parametrize("count", [0, 2])
def test_render_requires_single_placeholder(tmp_path, count):
    tpl = tmp_path / "t.yaml"
    tpl.write_text("Code:\n  ZipFile: |\n" + "    #HANDLER_SOURCE#\n" * count + "x: 1\n")
    handler = tmp_path / "h.py"
    handler.write_text("print(1)\n")
    with pytest.raises(ValueError):
        render(tpl, handler)


def test_handler_is_index_handler(doc):
    props = doc["Resources"]["InstallerFunction"]["Properties"]
    assert props["Handler"] == "index.handler"
    assert props["Runtime"] == "python3.13"


def test_deployment_service_timeout_is_3600(doc):
    assert doc["Resources"]["Deployment"]["Properties"]["ServiceTimeout"] == 3600


def test_buckets_retained(doc):
    for name in ("StateBucket", "ArtifactBucket"):
        res = doc["Resources"][name]
        assert res["DeletionPolicy"] == "Retain"
        assert res["UpdateReplacePolicy"] == "Retain"


def test_build_timeout_is_55(doc):
    assert doc["Resources"]["DeployProject"]["Properties"]["TimeoutInMinutes"] == 55


def test_build_queue_timeout_keeps_build_within_service_timeout(doc):
    props = doc["Resources"]["DeployProject"]["Properties"]
    # 5 is CodeBuild's minimum queue timeout; queue + build fit in the 60-minute ServiceTimeout.
    assert props["QueuedTimeoutInMinutes"] == 5
    assert props["QueuedTimeoutInMinutes"] + props["TimeoutInMinutes"] <= 60


def test_build_rule_matches_every_terminal_status(doc):
    # TIMED_OUT and FAULT must reach the function too, or CloudFormation waits out the hour.
    pattern = doc["Resources"]["BuildFinishedRule"]["Properties"]["EventPattern"]
    assert pattern["detail"]["build-status"] == [{"anything-but": ["IN_PROGRESS"]}]


def _statements(doc):
    for res in doc["Resources"].values():
        if res["Type"] != "AWS::IAM::Role":
            continue
        for policy in res["Properties"].get("Policies", []):
            yield from policy["PolicyDocument"]["Statement"]


def _actions(stmt):
    actions = stmt["Action"]
    return [actions] if isinstance(actions, str) else actions


def test_no_governance_bypass(doc):
    # Defence in depth: the deploy role denies it explicitly, and nothing allows it by name.
    stmts = list(_statements(doc))
    assert not any(
        s["Effect"] == "Allow" and "s3:BypassGovernanceRetention" in _actions(s) for s in stmts
    )
    policies = doc["Resources"]["DeployRole"]["Properties"]["Policies"]
    deny = [
        s
        for p in policies
        for s in p["PolicyDocument"]["Statement"]
        if s["Effect"] == "Deny" and _actions(s) == ["s3:BypassGovernanceRetention"]
    ]
    assert len(deny) == 1
    assert deny[0]["Resource"] == "*"


def test_environment_always_passed(doc):
    props = doc["Resources"]["Deployment"]["Properties"]
    assert "Environment" in props


def test_buildspec_checksum_mismatch_writes_last_error(doc):
    spec = doc["Resources"]["DeployProject"]["Properties"]["Source"]["BuildSpec"]
    script = yaml.safe_load(spec)["phases"]["build"]["commands"][0]
    assert "Uploaded release does not match the expected checksum" in script
    assert "\\u2014 re-upload the file from the GitHub Release." in script
    assert "ssm put-parameter --name /aphrodite/installer/last-error --overwrite" in script
    assert "$CODEBUILD_BUILD_ID" in script
    assert "bash installer/deploy.sh" in script
