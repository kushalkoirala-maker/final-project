from flask_login import LoginManager

login_manager = LoginManager()
login_manager.login_view = "web.login"

from .models.user import User  # noqa: E402

@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))
