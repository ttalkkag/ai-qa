from django import forms

from .models import User


class LoginForm(forms.Form):
    username = forms.CharField(label="아이디", max_length=150, widget=forms.TextInput(attrs={"autocomplete": "username"}))
    password = forms.CharField(label="비밀번호", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}), strip=False)


class PasswordForm(forms.Form):
    current_password = forms.CharField(label="현재 비밀번호", widget=forms.PasswordInput, strip=False)
    new_password = forms.CharField(label="새 비밀번호", widget=forms.PasswordInput, strip=False)
    new_password_confirm = forms.CharField(label="새 비밀번호 확인", widget=forms.PasswordInput, strip=False)

    def clean(self):
        data = super().clean()
        if data.get("new_password") != data.get("new_password_confirm"):
            self.add_error("new_password_confirm", "새 비밀번호가 일치하지 않습니다.")
        return data


class ProfileForm(forms.Form):
    display_name = forms.CharField(label="닉네임", max_length=100)


class CreateAccountForm(forms.Form):
    username = forms.CharField(label="사용자 이름", max_length=150)
    display_name = forms.CharField(label="표시 이름", max_length=100)
    role = forms.ChoiceField(label="역할", choices=User.Role.choices)
    initial_password = forms.CharField(label="초기 비밀번호", widget=forms.PasswordInput, strip=False)


class ChangeAccountForm(forms.Form):
    target_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_authz_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    display_name = forms.CharField(label="표시 이름", max_length=100)
    role = forms.ChoiceField(label="역할", choices=User.Role.choices)
    is_active = forms.BooleanField(label="활성 계정", required=False)
    reason = forms.CharField(label="변경 사유", max_length=500)


class ResetPasswordForm(forms.Form):
    target_id = forms.UUIDField(widget=forms.HiddenInput)
    new_temporary_password = forms.CharField(label="새 초기 비밀번호", widget=forms.PasswordInput, strip=False)
    reason = forms.CharField(label="재설정 사유", max_length=500)


class MembershipForm(forms.Form):
    user_id = forms.UUIDField(widget=forms.HiddenInput)
    project_id = forms.ChoiceField(label="프로젝트")
    granted = forms.ChoiceField(label="할당", choices=[("yes", "추가"), ("no", "회수")])
    reason = forms.CharField(label="변경 사유", max_length=500)

    def __init__(self, *args, projects=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project_id"].choices = [(str(project.pk), project.name) for project in projects]
