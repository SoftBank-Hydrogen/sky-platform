from domain.access import Action, LoginSource, Principal, ResourceOwner, Role, owner_from_record, permitted
from interfaces.http.auth import LocalTokenAuthenticator


def principal(organization: str, role: Role, source: LoginSource = LoginSource.EXTERNAL_IDP) -> Principal:
    return Principal("user_1", organization, role, source)


def test_an_external_login_cannot_read_another_organization_even_with_admin_role():
    owner = ResourceOwner("team_a", "creator_1")
    for role in Role:
        for action in Action:
            assert not permitted(principal("team_b", role), action, owner)


def test_corporate_login_does_not_grant_extra_permissions():
    owner = ResourceOwner("team_a", "creator_1")
    viewer = principal("team_a", Role.VIEWER, LoginSource.CORPORATE_SSO)
    assert permitted(viewer, Action.READ, owner)
    assert not permitted(viewer, Action.DEPLOY, owner)
    assert not permitted(viewer, Action.RETIRE, owner)


def test_membership_role_controls_actions_within_the_same_organization():
    owner = ResourceOwner("team_a", "creator_1")
    deployer = principal("team_a", Role.DEPLOYER)
    admin = principal("team_a", Role.ADMIN)
    assert permitted(deployer, Action.DEPLOY, owner)
    assert not permitted(deployer, Action.RETIRE, owner)
    assert not permitted(deployer, Action.MANAGE_MEMBERS, owner)
    assert all(permitted(admin, action, owner) for action in Action)


def test_unowned_legacy_record_cannot_be_claimed_by_a_logged_in_user():
    admin = principal("team_a", Role.ADMIN)
    assert owner_from_record({"id": "legacy"}) is None
    assert not permitted(admin, Action.READ, owner_from_record({"id": "legacy"}))
    assert not permitted(None, Action.READ, ResourceOwner("team_a", "creator_1"))
    assert owner_from_record({"organization_id": "team_a", "created_by": "creator_1"}) == ResourceOwner(
        "team_a", "creator_1")


def test_local_token_authentication_is_single_workspace_and_rejects_other_tokens():
    authenticator = LocalTokenAuthenticator("secret-value")
    assert authenticator.authenticate(None) is None
    assert authenticator.authenticate("other-value") is None
    assert authenticator.authenticate("secret-value") == Principal(
        "local_operator", "local_workspace", Role.ADMIN, LoginSource.LOCAL)
