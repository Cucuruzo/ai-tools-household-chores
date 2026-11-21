from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('family/', views.family, name='family'),
    path('family/add/', views.member_add, name='member_add'),
    path('family/<int:pk>/edit/', views.member_edit, name='member_edit'),
    path('family/<int:pk>/move/', views.member_move, name='member_move'),
    path('family/<int:pk>/delete/', views.member_delete, name='member_delete'),
    path('chores/create/', views.chore_create, name='chore_create'),
    path('chores/<int:pk>/edit/', views.chore_edit, name='chore_edit'),
    path('profile/<int:pk>/', views.profile_switch, name='profile_switch'),
]
