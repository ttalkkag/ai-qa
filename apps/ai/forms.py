from django import forms


class ProfileForm(forms.Form):
    key = forms.ChoiceField(label="목적", choices=[("conversation", "질문·요구사항 정리"), ("implementation", "코드 수정"), ("implementation_deep", "관리자 심층 수정")])
    model_id = forms.CharField(label="모델 ID", max_length=100)
    effort = forms.ChoiceField(label="Effort", choices=[(value, value) for value in ("minimal", "low", "medium", "high", "xhigh", "ultra")])
    enabled = forms.BooleanField(label="검증 이후 이 프로필 사용", required=False, initial=True)
