import pytest
from django.urls import reverse

from apps.ai.models import AIInvocation, ModelProfile
from apps.runs.models import Job
from tests.django.test_ai_local import admin, initial_profiles
from tests.django.test_projects import login


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('consent', [None, '', 'no', 'yes'])
def test_model_validation_keeps_server_confirmation_gate(client, admin, consent):
    login(client, admin)
    profile = ModelProfile.objects.filter(key='conversation').first()
    data = {'key': 'modal-validation'}
    if consent is not None:
        data['consent'] = consent
    response = client.post(reverse('ai:validate-profile', args=[profile.pk]), data)
    if consent == 'yes':
        assert response.status_code == 302
        assert AIInvocation.objects.count() == Job.objects.count() == 1
    else:
        assert response.status_code == 400
        assert 'CONSENT_REQUIRED' in response.content.decode()
        assert not AIInvocation.objects.exists()
        assert not Job.objects.exists()


def test_model_validation_uses_modal_but_keeps_profile_setting(client, admin):
    login(client, admin)
    content = client.get(reverse('ai:settings')).content.decode()
    assert 'name="consent" value="" data-confirm-value="yes"' in content
    assert '실제 이용량이 발생할 수 있음에 동의' not in content
    assert 'inert data-confirm=' in content
    assert 'id="confirmation-dialog"' in content
    assert 'name="enabled"' in content
