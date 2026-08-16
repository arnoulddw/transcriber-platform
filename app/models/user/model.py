import logging
from typing import Optional, Dict, Any

from flask_login import UserMixin
from mysql.connector import Error as MySQLError

from app.database import get_cursor

logger = logging.getLogger(__name__)

try:
    from app.models.role import Role
except ImportError as e:
    logger.critical(f"[DB:Models:User] Failed to import Role model dependencies: {e}. This may cause runtime errors.")
    Role = None  # type: ignore


class User(UserMixin):
    id: int
    username: str
    email: str
    password_hash: Optional[str]
    role_id: Optional[int]
    created_at: str
    api_keys_encrypted: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    oauth_provider: Optional[str]
    oauth_provider_id: Optional[str]
    default_content_language: Optional[str]
    default_transcription_model: Optional[str]
    default_openrouter_model: Optional[str]
    enable_auto_title_generation: bool
    language: Optional[str]
    public_api_key_hash: Optional[str]
    public_api_key_last_four: Optional[str]
    public_api_key_created_at: Optional[str]
    _role: Optional['Role']

    def __init__(
        self,
        id: int,
        username: str,
        email: str,
        password_hash: Optional[str],
        role_id: Optional[int],
        created_at: str,
        api_keys_encrypted: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        oauth_provider: Optional[str] = None,
        oauth_provider_id: Optional[str] = None,
        default_content_language: Optional[str] = None,
        default_transcription_model: Optional[str] = None,
        enable_auto_title_generation: bool = False,
        language: Optional[str] = None,
        public_api_key_hash: Optional[str] = None,
        public_api_key_last_four: Optional[str] = None,
        public_api_key_created_at: Optional[str] = None,
        default_openrouter_model: Optional[str] = None,
    ):
        self.id = id
        self.username = username
        self.email = email
        self.password_hash = password_hash
        self.role_id = role_id
        self.created_at = created_at
        self.api_keys_encrypted = api_keys_encrypted
        self.first_name = first_name
        self.last_name = last_name
        self.oauth_provider = oauth_provider
        self.oauth_provider_id = oauth_provider_id
        self.default_content_language = default_content_language
        self.default_transcription_model = default_transcription_model
        self.default_openrouter_model = default_openrouter_model
        self.enable_auto_title_generation = enable_auto_title_generation
        self.language = language
        self.public_api_key_hash = public_api_key_hash
        self.public_api_key_last_four = public_api_key_last_four
        self.public_api_key_created_at = public_api_key_created_at
        self._role = None

    @property
    def role(self) -> Optional['Role']:
        try:
            logger.debug(f"[User:{self.id}] role property accessed. cached={self._role is not None}, role_id={self.role_id}")
        except Exception:
            pass
        if self._role is None and self.role_id is not None:
            from app.models.role import _map_row_to_role

            sql = 'SELECT * FROM roles WHERE id = %s'
            cursor = None
            try:
                cursor = get_cursor()
                cursor.execute(sql, (self.role_id,))
                row = cursor.fetchone()
                self._role = _map_row_to_role(row)
                if self._role:
                    logger.debug(
                        f"[User:{self.id}] Loaded role snapshot from DB. role_id={self.role_id}, role_name={getattr(self._role, 'name', None)}"
                    )
                else:
                    logger.warning(f"[User:{self.id}] No role found for role_id={self.role_id}.")
            except MySQLError as err:
                logger.error(f"[User:{self.id}] Error fetching role (ID: {self.role_id}): {err}", exc_info=True)
                self._role = None
            finally:
                if cursor:
                    pass
        elif self.role_id is None:
            logger.warning(f"[User:{self.id}] User has no role_id assigned.")
        return self._role

    def has_permission(self, permission_name: str) -> bool:
        return self.role.has_permission(permission_name) if self.role else False

    def get_limit(self, limit_name: str) -> int:
        return self.role.get_limit(limit_name) if self.role else 0

    def get_total_minutes(self) -> float:
        """Calculates the total transcription minutes used by the user."""
        from app.models.user_utils import get_user_usage_stats

        stats = get_user_usage_stats(self.id)
        return stats.get('total_minutes', 0.0)

    def __repr__(self):
        role_info = f"Role:{self.role.name}" if self.role else f"RoleID:{self.role_id}"
        oauth_info = f", Provider:{self.oauth_provider}" if self.oauth_provider else ""
        name_info = f", Name: {self.first_name or ''} {self.last_name or ''}".strip() if self.first_name or self.last_name else ""
        return f'<User {self.username} (ID: {self.id}, Email: {self.email}{name_info}, {role_info}{oauth_info})>'


def _map_row_to_user(row: Dict[str, Any]) -> Optional[User]:
    if row:
        required_fields = ['id', 'username', 'email', 'created_at']
        if not all(field in row for field in required_fields):
            logger.error(f"[DB:User] Database row missing required fields for User object: {row}")
            return None
        user = User(
            id=row['id'],
            username=row['username'],
            email=row['email'],
            password_hash=row.get('password_hash'),
            role_id=row.get('role_id'),
            created_at=row['created_at'],
            api_keys_encrypted=row.get('api_keys_encrypted'),
            first_name=row.get('first_name'),
            last_name=row.get('last_name'),
            oauth_provider=row.get('oauth_provider'),
            oauth_provider_id=row.get('oauth_provider_id'),
            default_content_language=row.get('default_content_language'),
            default_transcription_model=row.get('default_transcription_model'),
            enable_auto_title_generation=bool(row.get('enable_auto_title_generation', False)),
            language=row.get('language'),
            public_api_key_hash=row.get('public_api_key_hash'),
            public_api_key_last_four=row.get('public_api_key_last_four'),
            public_api_key_created_at=row.get('public_api_key_created_at'),
            default_openrouter_model=row.get('default_openrouter_model'),
        )
        return user
    return None
