from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class LimitedAttachmentUploadHandler(FileUploadHandler):
    def __init__(self, request=None):
        super().__init__(request)
        self.file_size = 0
        self.total_size = 0
        self.file_count = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.file_size = 0
        self.file_count += 1
        if self.file_count > 3:
            self.request.attachment_upload_rejected = True
            raise StopUpload(connection_reset=True)

    def receive_data_chunk(self, raw_data, start):
        self.file_size += len(raw_data)
        self.total_size += len(raw_data)
        if self.file_size > 5 * 1024 * 1024 or self.total_size > 15 * 1024 * 1024:
            self.request.attachment_upload_rejected = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None


class AttachmentUploadLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path.startswith("/conversations/") and request.path.endswith("/attachments/"):
            request.upload_handlers.insert(0, LimitedAttachmentUploadHandler(request))
        return self.get_response(request)
