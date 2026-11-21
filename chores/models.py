from django.db import models
from django.db.models import Max

AVATAR_CHOICES = [
    (emoji, emoji)
    for emoji in [
        "😀", "😎", "🤓", "🥳", "👩", "👨", "👧", "👦",
        "👵", "👴", "🐱", "🐶", "🦊", "🐼", "🐸", "🦄",
    ]
]


class Member(models.Model):
    class Role(models.TextChoices):
        ADULT = "adult", "Adult"
        KID = "kid", "Kid"

    name = models.CharField(max_length=50)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.ADULT)
    avatar = models.CharField(max_length=8, choices=AVATAR_CHOICES)
    order = models.PositiveIntegerField(blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.avatar} {self.name}"

    def save(self, *args, **kwargs):
        if self.order is None:
            current_max = Member.objects.aggregate(m=Max("order"))["m"]
            self.order = 0 if current_max is None else current_max + 1
        super().save(*args, **kwargs)
