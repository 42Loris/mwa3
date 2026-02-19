# monitoring/views.py

from django.http import JsonResponse
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.contrib.auth.models import User
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

def active_users_count(request):
    try:
        now = timezone.now()
        # Filter for sessions that have not expired
        active_sessions = Session.objects.filter(expire_date__gte=now)
        active_user_count = active_sessions.count()
        logger.info(f"Active user count: {active_user_count}")
        return JsonResponse({'active_user_count': active_user_count})
    except Exception as e:
        logger.error(f"Error in active_users_count: {e}")
        return JsonResponse({'error': 'An error occurred'}, status=500)


# get active users
def get_active_users(request):
    try:
        now = timezone.now()
        active_sessions = Session.objects.filter(expire_date__gte=now)
        
        user_data = []
        for session in active_sessions:
            data = session.get_decoded()  # Decode session data
            user_id = data.get('_auth_user_id')  # Extract user ID
            if user_id:
                try:
                    user = User.objects.get(id=user_id)
                    last_activity = session.expire_date - timezone.timedelta(seconds=settings.SESSION_COOKIE_AGE)
                    
                    user_data.append({
                        "id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "first_name": user.first_name,
                        "last_name": user.last_name,
                        "last_activity": last_activity.strftime('%Y-%m-%d %H:%M:%S')
                    })
                except User.DoesNotExist:
                    logger.warning(f"User {user_id} not found")

        return JsonResponse({'active_users': user_data}, json_dumps_params={"ensure_ascii": False})
    except Exception as e:
        logger.error(f"Error in get_active_users: {e}")
        return JsonResponse({'error': 'An error occurred'}, status=500)