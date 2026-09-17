from django import forms


class DraftForm(forms.Form):
    expected_draft_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    title = forms.CharField(label="제목", max_length=200, required=False)
    goal = forms.CharField(label="목표", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    current_behavior = forms.CharField(label="현재 동작", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    expected_behavior = forms.CharField(label="기대 동작", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    reproduction_steps = forms.CharField(label="재현 방법 또는 미확인 사유", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    acceptance_criteria = forms.CharField(label="완료 조건 (한 줄에 하나)", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    scope = forms.CharField(label="수정 범위", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    out_of_scope = forms.CharField(label="제외 범위", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    related_domains = forms.CharField(label="관련 도메인", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    impact_analysis = forms.CharField(label="영향 범위 (코드·함수·엔드포인트·데이터)", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    conflict_analysis = forms.CharField(label="충돌·의도하지 않은 영향 검토", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    unknowns = forms.CharField(label="미확인 사실과 영향", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    blocking_questions = forms.CharField(label="먼저 해결할 필수 질문 (해결한 질문은 제거)", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    attachment_ids = forms.MultipleChoiceField(label="대화의 참고자료 선택", required=False, widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, conversation=None, **kwargs):
        super().__init__(*args, **kwargs)
        if conversation is not None:
            from apps.artifacts.models import Artifact
            self.fields["attachment_ids"].choices = [
                (str(artifact.id), artifact.display_name)
                for artifact in Artifact.objects.filter(conversation=conversation, kind="INPUT_ATTACHMENT", visibility="NORMAL", expired_at__isnull=True)
            ]

    @classmethod
    def for_request(cls, work_request):
        initial = dict(work_request.draft)
        for field in ("acceptance_criteria", "blocking_questions"):
            initial[field] = "\n".join(initial.get(field, []))
        initial["expected_draft_version"] = work_request.draft_version
        return cls(initial=initial, conversation=work_request.conversation)

    def spec_fields(self):
        fields = dict(self.cleaned_data)
        fields.pop("expected_draft_version")
        for field in ("acceptance_criteria", "blocking_questions"):
            fields[field] = fields[field].splitlines()
        return fields
