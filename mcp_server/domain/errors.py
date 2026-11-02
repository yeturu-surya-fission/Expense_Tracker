class UserError(Exception):
    """An expected, user-facing problem (bad input, unknown friend, nothing to settle...). The message is shown as is."""


class ExpenseError(UserError):
    pass


class FriendError(UserError):
    pass


class SplitError(UserError):
    pass


class GroupError(UserError):
    pass


class DebtError(UserError):
    pass
