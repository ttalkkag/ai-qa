from django import forms

from .models import PublicationPolicy


class PublicationPolicyForm(forms.ModelForm):
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    remote_url = forms.URLField(assume_scheme="https", required=False, label="HTTPS 원격 URL")

    class Meta:
        model = PublicationPolicy
        fields = ["enabled", "repository_id", "repository_full_name", "remote_url", "base_branch", "mode", "commit_author_name", "commit_author_email"]
        labels = {
            "enabled": "선택적 GitHub 발행 사용", "repository_id": "GitHub 저장소 ID", "repository_full_name": "저장소 (owner/repo)",
            "remote_url": "HTTPS 원격 URL", "base_branch": "기준 브랜치", "mode": "발행 범위",
            "commit_author_name": "커밋 작성자 이름", "commit_author_email": "커밋 작성자 이메일",
        }


class DecisionForm(forms.Form):
    snapshot_hash = forms.RegexField(r"^[a-f0-9]{64}$", widget=forms.HiddenInput)
    idempotency_key = forms.CharField(max_length=128, widget=forms.HiddenInput)
    reason = forms.CharField(max_length=500, required=False, label="사유", widget=forms.Textarea(attrs={"rows": 2}))
