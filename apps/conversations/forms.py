from django import forms


class StartConversationForm(forms.Form):
    body = forms.CharField(label="질문 또는 현상·목표", max_length=10000, help_text="현재 상황과 원하는 결과를 설명해 주세요 대화만으로 코드 수정이나 발행이 시작되지는 않습니다", widget=forms.Textarea(attrs={"rows": 5}))
    client_message_id = forms.CharField(max_length=128, widget=forms.HiddenInput)
    creation_key = forms.CharField(max_length=128, widget=forms.HiddenInput)


class MessageForm(forms.Form):
    body = forms.CharField(label="추가 설명", max_length=10000, widget=forms.Textarea(attrs={"rows": 4}))
    client_message_id = forms.CharField(max_length=128, widget=forms.HiddenInput)
    attachment_ids = forms.CharField(label="첨부 ID", required=False, widget=forms.HiddenInput)

    def clean_attachment_ids(self):
        return [value.strip() for value in self.cleaned_data.get("attachment_ids", "").split(",") if value.strip()]


class RenameForm(forms.Form):
    title = forms.CharField(label="대화 제목", max_length=120)
