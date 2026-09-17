class DomainError(Exception):
    def __init__(self, code, message=None, status=400):
        self.code = code
        self.message = message or "요청을 처리할 수 없습니다. 입력과 현재 상태를 확인해 주세요."
        self.status = status
        super().__init__(self.message)
