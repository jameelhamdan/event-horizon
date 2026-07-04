"""API views — newsletter subscribe, confirm, unsubscribe, list, detail."""
import logging
import uuid

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from newsletter.models import DailyNewsletter, Subscriber
from ..serializers import (
    NewsletterDetailSerializer,
    NewsletterListSerializer,
    SubscribeSerializer,
)
from services.email.mailer import send_confirmation_email

logger = logging.getLogger(__name__)


class SubscribeThrottle(AnonRateThrottle):
    rate = '5/hour'


class SubscribeView(APIView):
    """POST /api/newsletter/subscribe/"""
    authentication_classes = []
    throttle_classes = [SubscribeThrottle]

    def post(self, request):
        serializer = SubscribeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data['email']
        existing = Subscriber.objects.filter(email=email).first()

        if existing:
            if existing.is_active:
                return Response(
                    {'detail': 'Please check your email to confirm your subscription.'},
                    status=status.HTTP_201_CREATED,
                )
            existing.token = uuid.uuid4()
            existing.is_active = False
            existing.confirmed_at = None
            existing.unsubscribed_at = None
            existing.save()
            sub = existing
        else:
            sub = Subscriber.objects.create(email=email)

        send_confirmation_email(sub)
        return Response(
            {'detail': 'Please check your email to confirm your subscription.'},
            status=status.HTTP_201_CREATED,
        )


class ConfirmView(APIView):
    """GET /api/newsletter/confirm/<token>/"""

    def get(self, request, token):
        try:
            sub = Subscriber.objects.get(token=token, is_active=False)
        except Subscriber.DoesNotExist:
            return Response(
                {'detail': 'Invalid or already confirmed link.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        sub.is_active = True
        sub.confirmed_at = timezone.now()
        sub.save()
        return Response({'detail': 'Subscription confirmed. Welcome aboard!'})


class UnsubscribeView(APIView):
    """GET /api/newsletter/unsubscribe/<token>/"""

    def get(self, request, token):
        try:
            sub = Subscriber.objects.get(token=token)
        except Subscriber.DoesNotExist:
            return Response(
                {'detail': 'Invalid unsubscribe link.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not sub.is_active:
            return Response({'detail': 'This email is already unsubscribed.'})

        sub.is_active = False
        sub.unsubscribed_at = timezone.now()
        sub.save()
        return Response({'detail': 'You have been unsubscribed successfully.'})


class NewsletterListView(APIView):
    """GET /api/newsletter/ — list sent newsletters"""

    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get('limit', 100)), 1), 500)
        except ValueError:
            limit = 100
        qs = DailyNewsletter.objects.filter(
            status=DailyNewsletter.STATUS_SENT
        ).order_by('-date')
        newsletters = qs[:limit]
        serializer = NewsletterListSerializer(newsletters, many=True)
        return Response({'results': serializer.data, 'count': qs.count()})


class NewsletterLatestView(APIView):
    """GET /api/newsletter/latest/ — retrieve the most recently sent newsletter"""

    def get(self, request):
        newsletter = DailyNewsletter.objects.filter(
            status=DailyNewsletter.STATUS_SENT
        ).first()
        if not newsletter:
            return Response(
                {'detail': 'No newsletter available.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(NewsletterDetailSerializer(newsletter).data)


class NewsletterDetailView(APIView):
    """GET /api/newsletter/<date>/ — retrieve a newsletter by date (YYYY-MM-DD)"""

    def get(self, request, date):
        try:
            newsletter = DailyNewsletter.objects.get(date=date)
        except (DailyNewsletter.DoesNotExist, ValueError):
            return Response(
                {'detail': 'Newsletter not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(NewsletterDetailSerializer(newsletter).data)

