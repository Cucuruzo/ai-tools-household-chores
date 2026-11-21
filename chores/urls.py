from django.urls import path

from . import contribution_views, history_views, progress_views, template_views, views

urlpatterns = [
    path('', views.home, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('progress/', progress_views.progress, name='progress'),
    path('calendar/', views.calendar, name='calendar'),
    path('family/', views.family, name='family'),
    path('family/add/', views.member_add, name='member_add'),
    path('family/<int:pk>/edit/', views.member_edit, name='member_edit'),
    path('family/<int:pk>/move/', views.member_move, name='member_move'),
    path('family/<int:pk>/delete/', views.member_delete, name='member_delete'),
    path('dashboard/toggle/<int:pk>/', views.occurrence_toggle, name='occurrence_toggle'),
    path('dashboard/assign/<int:pk>/', views.occurrence_assign, name='occurrence_assign'),
    path('dashboard/assign/planned/<int:chore_id>/', views.planned_assign, name='planned_assign'),
    path('chores/create/', views.chore_create, name='chore_create'),
    path('chores/<int:pk>/edit/', views.chore_edit, name='chore_edit'),
    path('chores/<int:pk>/delete/', views.chore_delete, name='chore_delete'),
    path('chores/templates/', template_views.template_list, name='template_list'),
    path('chores/templates/<int:pk>/edit/', template_views.template_edit, name='template_edit'),
    path('chores/templates/<int:pk>/delete/', template_views.template_delete, name='template_delete'),
    path('contribution/', contribution_views.contribution, name='contribution'),
    path('history/', history_views.history, name='history'),
    path('profile/<int:pk>/', views.profile_switch, name='profile_switch'),
]
