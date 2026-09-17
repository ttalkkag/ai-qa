import re

from django import forms
from django.template.loader import render_to_string

from apps.projects.forms import RegisterProjectForm


def test_registration_fields_keep_help_without_consent_checkbox():
    content = render_to_string("includes/form_fields.html", {"form": RegisterProjectForm()})
    assert content.count('class="form-field"') == 4
    assert '<label for="id_name">Name*:</label>' in content
    assert '<label for="id_description">Description:</label>' in content
    assert '<label for="id_local_path">Path*:</label>' in content
    assert '<label for="id_source_ref">Branch:</label>' in content
    assert 'type="checkbox"' not in content
    assert 'confirm_scope' not in content
    assert 'aria-describedby="id_source_ref_helptext"' in content
    assert 'id="id_source_ref_helptext"' in content
    assert RegisterProjectForm().fields['name'].help_text == "관리자와 매니저가 목록에서 구분할 수 있는 이름을 입력"
    assert RegisterProjectForm().fields['description'].help_text == "선택 사항 서비스의 용도와 담당 업무를 간단하게 설명"
    assert "develop → main → master → 현재 브랜치 순으로 선택" in RegisterProjectForm().fields['source_ref'].help_text
    assert "입력한 브랜치가 없으면" in content and "새 로컬 브랜치를 생성" in content
    assert "GitHub URL이 아닌 서버 PC의 Git 최상위 폴더를 입력<br>(Git 저장소와 최소 한 번 커밋된 코드가 필요하며, 원격에만 있다면 먼저 서버 PC에 내려받으세요)" in content


def test_actual_setting_checkbox_keeps_its_label():
    class SettingsForm(forms.Form):
        enabled = forms.BooleanField(label='설정 사용', required=False, initial=True)
    content = render_to_string('includes/form_fields.html', {'form': SettingsForm()})
    assert '<div class="checkbox-field">' in content
    assert '<label for="id_enabled">설정 사용:</label>' in content
    assert 'checked' in content


def test_shared_fields_preserve_hidden_values_and_validation_feedback():
    class ExampleForm(forms.Form):
        title = forms.CharField(label="제목", help_text="제목을 입력하세요.")
        revision = forms.IntegerField(widget=forms.HiddenInput, initial=3)

    valid_content = render_to_string("includes/form_fields.html", {"form": ExampleForm()})
    assert '<input type="hidden" name="revision" value="3" id="id_revision">' in valid_content
    assert 'for="id_revision"' not in valid_content
    assert valid_content.count('class="form-field"') == 1

    form = ExampleForm({"title": "", "revision": "invalid"})
    assert not form.is_valid()
    form.add_error(None, "저장할 수 없습니다.")
    content = render_to_string("includes/form_fields.html", {"form": form})
    assert "저장할 수 없습니다." in content
    assert str(form.errors["title"][0]) in content
    assert str(form.errors["revision"][0]) in content
    assert 'aria-invalid="true"' in content
    title = re.search(r'<input\b[^>]*name="title"[^>]*>', content).group(0)
    described = re.search(r'aria-describedby="([^"]+)"', title).group(1).split()
    assert "id_title_helptext" in described
    assert "id_title_error" in described
    for target in described:
        assert f'id="{target}"' in content


def test_shared_multiple_checkbox_field_keeps_group_label_and_selected_values():
    class AttachmentForm(forms.Form):
        attachments = forms.MultipleChoiceField(
            label="함께 전달할 자료", choices=[("first", "기획서"), ("second", "화면 자료")],
            widget=forms.CheckboxSelectMultiple, help_text="필요한 자료만 선택하세요.",
        )

    form = AttachmentForm({"attachments": ["first"]})
    assert form.is_valid()
    content = render_to_string("includes/form_fields.html", {"form": form})
    assert '<fieldset class="form-field choice-field"' in content
    assert '<legend>함께 전달할 자료:</legend>' in content
    assert 'aria-describedby="id_attachments_helptext"' in content
    assert 'id="id_attachments_helptext"' in content
    for index in (0, 1):
        assert f'for="id_attachments_{index}"' in content
        assert f'id="id_attachments_{index}"' in content
    first = re.search(r'<input\b[^>]*value="first"[^>]*>', content).group(0)
    second = re.search(r'<input\b[^>]*value="second"[^>]*>', content).group(0)
    assert 'checked' in first
    assert 'checked' not in second


def test_shared_fields_escape_field_labels_and_entered_values():
    class ExampleForm(forms.Form):
        title = forms.CharField(label='<img src=x onerror="bad()">')

    content = render_to_string("includes/form_fields.html", {
        "form": ExampleForm({"title": '<script>bad()</script> & "quote"'}),
    })
    assert "<img" not in content
    assert "<script>" not in content
    assert "&lt;img" in content
    assert "&lt;script&gt;bad()&lt;/script&gt;" in content
    assert "&amp;" in content
