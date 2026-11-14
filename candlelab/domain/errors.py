class AppError(Exception):
    def __init__(self, code, message, details=None, status=422):
        self.code = code
        self.message = message
        self.details = details or []
        self.status = status
        super().__init__(message)

    def body(self):
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


def invalid(field, message, row=None):
    detail = {"field": field, "reason": message}
    if row is not None:
        detail["row"] = row
    return AppError("VALIDATION_ERROR", message, [detail])
