import logging
from django.http import HttpResponse, HttpResponseBadRequest
from django.db import transaction
from django.views.decorators.http import require_POST
from opaque_keys.edx.keys import CourseKey
from openedx.core.lib.courses import get_course_by_id
from lms.djangoapps.certificates import api as certs_api
from lms.djangoapps.courseware.views.views import is_course_passed
from django.utils.translation import gettext_lazy as _
# from lms.djangoapps.certificates.exceptions import CertificateGenerationNotAllowed
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.djangoapps.user_authn.views.register import _track_user_registration as original_track_user_registration
from common.djangoapps.course_modes.models import CourseMode
from xmodule.modulestore.django import modulestore

from django.conf import settings
import datetime
from pytz import UTC

from common.djangoapps.track import segment

logger = logging.getLogger(__name__)

@require_POST
@transaction.non_atomic_requests
def custom_generate_user_cert(request, course_id):
    """
    Request that a course certificate be generated for the user.

    Args:
        request (HttpRequest): The POST request to this view.
        course_id (unicode): The identifier for the course.
    Returns:
        HttpResponse: 200 on success, 400 if a new certificate cannot be generated.
    """
    if not request.user.is_authenticated:
        logger.info("Anon user trying to generate certificate for %s", course_id)
        return HttpResponseBadRequest(
            _('You must be signed in to {platform_name} to create a certificate.').format(
                platform_name=configuration_helpers.get_value('PLATFORM_NAME', settings.PLATFORM_NAME)
            )
        )

    student = request.user
    course_key = CourseKey.from_string(course_id)

    try:
        course = get_course_by_id(course_key)
    except Exception:
        return HttpResponseBadRequest(_("Course is not valid"))

    if not is_course_passed(course, student):
        return HttpResponseBadRequest(_("Your certificate will be available when you pass the course."))

    certificate_status = certs_api.certificate_downloadable_status(student, course.id)

    if certificate_status["is_downloadable"]:
        return HttpResponseBadRequest(_("Certificate has already been created."))
    elif certificate_status["is_generating"]:
        return HttpResponseBadRequest(_("Certificate is being created."))

    logger.info(
        "Attempt will be made to generate a course certificate for student %s in course %s",
        student.id,
        course_key
    )

    try:
        certs_api.generate_user_certificates(student, course_key, course=course)
    except Exception as exc:
        logger.critical(
            "Certificate generation failed for user %s, course %s with error: %s",
            student.id,
            course_key,
            str(exc)
        )
        return HttpResponseBadRequest(_("An error occurred while creating your certificate."))

    return HttpResponse()


def custom_track_user_registration(user, profile, params, third_party_provider, registration, is_marketable):
    """
    Custom version of track_user_registration with additional market field tracking.
    This function is intended to be used as a monkey patch for the original _track_user_registration.
    """
    # First, call the original function to maintain base functionality
    original_track_user_registration(user, profile, params, third_party_provider, registration, is_marketable)

    # Now add our custom tracking logic
    if hasattr(settings, 'LMS_SEGMENT_KEY') and settings.LMS_SEGMENT_KEY:
        try:
            market = user.extrainfo.market
            extrainfo = {'market': market}
        except Exception as e:
            logger.exception("Exception in extrainfo_dict: %s", e)
            extrainfo = ''

        # Update traits with our custom data
        traits = {
            'email': user.email,
            'username': user.username,
            'name': profile.name,
            'age': profile.age or -1,
            'yearOfBirth': profile.year_of_birth or datetime.datetime.now(UTC).year,
            'education': profile.level_of_education_display,
            'address': profile.mailing_address,
            'gender': profile.gender_display,
            'country': str(profile.country),
            'is_marketable': is_marketable,
            'extrainfo': extrainfo
        }

        # Identify the user with the updated traits
        segment.identify(user.id, traits)

        # Track the custom event
        segment.track(
            user.id,
            "edx.bi.user.account.registered.custom",
            properties={
                'category': 'conversion',
                'email': user.email,
                'username': user.username,
                'market': market if extrainfo else None
            }
        )

def custom_is_eligible_for_certificate(cls, mode_slug, status=None):
    """
    Custom version of the method that considers all modes eligible for certificate.
    """
    logger.info(f"Custom is_eligible_for_certificate called with mode_slug: {mode_slug}")
    return True

def apply_monkey_patch():
    # Apply monkey patch for is_eligible_for_certificate
    logger.info("Applying monkey patch for CourseMode.is_eligible_for_certificate")
    CourseMode.is_eligible_for_certificate = classmethod(custom_is_eligible_for_certificate)
    
    # Apply the patch for _track_user_registration
    from openedx.core.djangoapps.user_authn.views import register
    register._track_user_registration = custom_track_user_registration
    
    # Apply the patch for generate_user_cert
    from lms.djangoapps.courseware.views import views
    views.generate_user_cert = custom_generate_user_cert
