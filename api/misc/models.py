from django.db import models
from django_mongodb_backend.managers import MongoManager


class EmailLog(models.Model):
    TYPE_CONFIRMATION = 'confirmation'
    TYPE_NEWSLETTER = 'newsletter'
    TYPE_CHOICES = [(TYPE_CONFIRMATION, 'Confirmation'), (TYPE_NEWSLETTER, 'Newsletter')]

    STATUS_SENT = 'sent'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [(STATUS_SENT, 'Sent'), (STATUS_FAILED, 'Failed')]

    to = models.CharField(max_length=254)
    subject = models.CharField(max_length=255)
    email_type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES)
    error = models.TextField(blank=True, default='')
    sent_at = models.DateTimeField(auto_now_add=True)

    objects = MongoManager()

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return f'[{self.email_type}] {self.to} — {self.status} @ {self.sent_at}'
