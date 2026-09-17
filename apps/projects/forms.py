from django import forms

from apps.errors import DomainError
from .services import BRANCH_INPUT_PATTERN, BRANCH_NAME_MESSAGE, DEFAULT_EXCLUDED_PATHS, validate_requested_source_ref


def validate_branch_name(value):
    try:
        validate_requested_source_ref(value)
    except DomainError as error:
        raise forms.ValidationError(error.message, code=error.code) from error


BRANCH_HELP = "선택 사항 미입력 시, 서버 PC에 있는 develop → main → master → 현재 브랜치 순으로 선택<br>입력한 브랜치가 없으면 같은 순서로 선택한 기준에서 새 로컬 브랜치를 생성 기존 파일·현재 브랜치는 변경하지 않으며 원격 push는 하지 않음<br>" + BRANCH_NAME_MESSAGE


class RegisterProjectForm(forms.Form):
    name = forms.CharField(label="Name*", max_length=120, help_text="관리자와 매니저가 목록에서 구분할 수 있는 이름을 입력")
    description = forms.CharField(label="Description", widget=forms.Textarea(attrs={"rows": 3}), required=False, max_length=4000,
                                  help_text="선택 사항 서비스의 용도와 담당 업무를 간단하게 설명")
    local_path = forms.RegexField(regex=r"^/", label="Path*", max_length=1024,
                                widget=forms.TextInput(attrs={"pattern": "/.*", "title": "/로 시작하는 서버 PC의 절대 경로를 입력하세요."}),
                                error_messages={"invalid": "/로 시작하는 서버 PC의 절대 경로를 입력하세요."},
                                help_text=(
                                    "GitHub URL이 아닌 서버 PC의 Git 최상위 폴더를 입력<br>"
                                    "(Git 저장소와 최소 한 번 커밋된 코드가 필요하며, 원격에만 있다면 먼저 서버 PC에 내려받으세요)"
                                    "<ul><li>macOS: <code>/Users/your-name/Projects/sample-board</code></li>"
                                    "<li>Windows: <code>C:\\Users\\your-name\\Projects\\sample-board</code></li></ul>"
                                ))
    source_ref = forms.CharField(label="Branch", max_length=255, required=False, validators=[validate_branch_name],
                                 widget=forms.TextInput(attrs={"pattern": BRANCH_INPUT_PATTERN, "title": BRANCH_NAME_MESSAGE}),
                                 help_text=BRANCH_HELP)


class ProjectConfigurationForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput(), min_value=1)
    local_path = forms.CharField(label="Path*", max_length=1024)
    source_ref = forms.CharField(label="Branch", max_length=255, required=False, validators=[validate_branch_name],
                                 widget=forms.TextInput(attrs={"pattern": BRANCH_INPUT_PATTERN, "title": BRANCH_NAME_MESSAGE}),
                                 help_text=BRANCH_HELP)
    read_paths = forms.CharField(label="읽기 허용 경로", widget=forms.Textarea(attrs={"rows": 3}), initial=".", help_text="프로젝트 상대 경로를 한 줄에 하나씩 입력합니다.")
    write_paths = forms.CharField(label="수정 허용 경로", widget=forms.Textarea(attrs={"rows": 3}), initial=".", required=False)
    excluded_paths = forms.CharField(label="제외 경로", widget=forms.Textarea(attrs={"rows": 6}), initial="\n".join(DEFAULT_EXCLUDED_PATHS), required=False)
    verification_commands = forms.JSONField(label="검증 명령 목록(JSON)", initial=[], required=False, widget=forms.Textarea(attrs={"rows": 16}),
                                            help_text="명령을 등록해도 검사를 실행한 것은 아닙니다. required:true인 명령이 최소 하나 있어야 수정 실행이 가능합니다.")
    preparation_commands = forms.JSONField(label="오프라인 준비 명령 목록(JSON)", initial=[], widget=forms.Textarea(attrs={"rows": 6}), required=False)

    def policy(self):
        data = self.cleaned_data
        return {
            "source_ref": data["source_ref"],
            **{key: [line.strip() for line in data[key].splitlines() if line.strip()] for key in ("read_paths", "write_paths", "excluded_paths")},
            "verification_commands": data["verification_commands"] or [],
            "preparation_commands": data["preparation_commands"] or [],
        }

    @classmethod
    def initial_for(cls, project, policy):
        return {
            "expected_version": project.project_config_version,
            "local_path": project.canonical_repo_path,
            "source_ref": policy["source_ref"],
            **{key: "\n".join(policy[key]) for key in ("read_paths", "write_paths", "excluded_paths")},
            **{key: policy[key] for key in ("verification_commands", "preparation_commands")},
        }


class ProjectDetailsForm(forms.Form):
    name = forms.CharField(label="Name*", max_length=120)
    description = forms.CharField(label="Description", widget=forms.Textarea(attrs={"rows": 3}), max_length=4000, required=False)


class ProjectStateForm(forms.Form):
    state = forms.ChoiceField(label="상태", choices=[("ACTIVE", "활성"), ("ARCHIVED", "아카이브")])
    reason = forms.CharField(label="변경 사유", max_length=500)


class DependencyBundleForm(forms.Form):
    local_path = forms.CharField(label="준비 자료 디렉터리 절대 경로", max_length=1024, help_text="미리 확보하고 검사한 의존성 폴더를 입력 앱은 다운로드·설치하지 않으며 등록 후 변경된 자료는 사용할 수 없습니다")
    lock_file_hash = forms.RegexField(label="lock 파일 SHA-256", regex=r"^[0-9a-f]{64}$", max_length=64)
    platform = forms.CharField(label="플랫폼", max_length=80, help_text="Docker 이미지의 플랫폼 예: linux-aarch64 또는 linux-x86_64")
    runtime = forms.CharField(label="runtime", max_length=80, help_text="Docker 이미지의 Python 버전 예: python-3.12")
