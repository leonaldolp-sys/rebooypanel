import os
import json
import logging
import threading
from django.conf import settings
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from .models import VideoPrompt
from .services.video_handler import VideoValidationError
from .tasks import process_video_task, normalize_markdown_formatting

logger = logging.getLogger(__name__)


def dispatch_videoprompt_task(prompt_id: int):
    """
    Dispatches Celery background worker task if Celery is enabled,
    falling back gracefully to local thread if broker is unreachable.
    """
    if getattr(settings, 'USE_CELERY', True):
        try:
            return process_video_task.delay(prompt_id)
        except Exception as e:
            logger.warning(f"Celery dispatch failed for VideoPrompt #{prompt_id} ({e}), running local thread fallback.")

    def fallback_worker():
        process_video_task(prompt_id)

    thread = threading.Thread(target=fallback_worker, daemon=True)
    thread.start()
    return None


@login_required
def studio_view(request):
    """
    Vista principal de Video to Prompt Studio.
    """
    history = VideoPrompt.objects.filter(user=request.user).order_by('-created_at')[:30]
    history_count = VideoPrompt.objects.filter(user=request.user).count()
        
    quota_info = {
        'is_unlimited': (request.user.role == 'superadmin' or getattr(request.user, 'is_unlimited_prompts', False)),
        'limit': request.user.daily_prompt_limit,
        'used_today': request.user.get_prompts_used_today(),
        'remaining': request.user.get_prompts_remaining_today(),
        'can_generate': request.user.can_generate_prompt(),
    }
        
    context = {
        'history': history,
        'history_count': history_count,
        'my_history': history,
        'my_history_count': history_count,
        'languages': VideoPrompt.LANGUAGE_CHOICES,
        'quota': quota_info,
    }
    return render(request, 'videoprompt/studio.html', context)


@login_required
@require_POST
def generate_prompt_ajax(request):
    """
    Endpoint AJAX para iniciar el análisis de videos (individual o por lote) con validación estricta de cuota diaria.
    Soporta múltiples URLs (separadas por saltos de línea/comas) y múltiples archivos subidos.
    """
    # 1. Validar cuota diaria inicial del usuario
    if not request.user.can_generate_prompt():
        return JsonResponse({
            'status': 'error',
            'message': f'Has alcanzado tu límite diario de {request.user.daily_prompt_limit} prompts. Tu cuota se reiniciará a medianoche.'
        }, status=429)

    input_type = request.POST.get('input_type')
    additional_context = request.POST.get('additional_context', '').strip()
    prompt_language = request.POST.get('prompt_language', 'es').strip()
    
    try:
        created_records = []

        if input_type == 'link':
            raw_urls = request.POST.get('video_urls') or request.POST.get('video_url', '')
            import re
            url_list = [u.strip() for u in re.split(r'[\r\n,]+', raw_urls) if u.strip()]

            if not url_list:
                return JsonResponse({'status': 'error', 'message': 'Por favor ingresa al menos un enlace de video válido.'}, status=400)

            # Validar cuota suficiente para la cantidad de URLs
            remaining_quota = request.user.get_prompts_remaining_today()
            is_unlimited = (request.user.role == 'superadmin' or getattr(request.user, 'is_unlimited_prompts', False))
            if not is_unlimited and len(url_list) > remaining_quota:
                return JsonResponse({
                    'status': 'error',
                    'message': f'Intentas procesar {len(url_list)} videos, pero solo te quedan {remaining_quota} prompts disponibles hoy.'
                }, status=429)

            for url in url_list:
                record = VideoPrompt.objects.create(
                    user=request.user,
                    video_url=url,
                    additional_context=additional_context,
                    prompt_language=prompt_language,
                    status='pending'
                )
                created_records.append(record)
                dispatch_videoprompt_task(record.id)

        elif input_type == 'file':
            file_list = request.FILES.getlist('video_files')
            if not file_list and 'video_file' in request.FILES:
                file_list = request.FILES.getlist('video_file')

            if not file_list:
                return JsonResponse({'status': 'error', 'message': 'Por favor selecciona al menos un archivo de video.'}, status=400)

            # Validar cuota suficiente para la cantidad de archivos
            remaining_quota = request.user.get_prompts_remaining_today()
            is_unlimited = (request.user.role == 'superadmin' or getattr(request.user, 'is_unlimited_prompts', False))
            if not is_unlimited and len(file_list) > remaining_quota:
                return JsonResponse({
                    'status': 'error',
                    'message': f'Intentas subir {len(file_list)} videos, pero solo te quedan {remaining_quota} prompts disponibles hoy.'
                }, status=429)

            for video_file in file_list:
                record = VideoPrompt.objects.create(
                    user=request.user,
                    video_file=video_file,
                    additional_context=additional_context,
                    prompt_language=prompt_language,
                    status='pending'
                )
                created_records.append(record)
                dispatch_videoprompt_task(record.id)
        else:
            return JsonResponse({'status': 'error', 'message': 'Tipo de entrada no válido.'}, status=400)

        prompt_ids = [r.id for r in created_records]
        return JsonResponse({
            'status': 'success',
            'prompt_ids': prompt_ids,
            'prompt_id': prompt_ids[0] if prompt_ids else None,
            'count': len(prompt_ids),
            'message': f'Procesamiento iniciado correctamente para {len(prompt_ids)} video(s).'
        })

    except VideoValidationError as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'Error del servidor: {str(e)}'}, status=500)


@login_required
def check_prompt_status_ajax(request, pk):
    """
    Polling de estado del prompt.
    """
    if request.user.role == 'superadmin':
        prompt_record = get_object_or_404(VideoPrompt, pk=pk)
    else:
        prompt_record = get_object_or_404(VideoPrompt, pk=pk, user=request.user)
        
    data = {
        'id': prompt_record.id,
        'status': prompt_record.status,
        'status_display': prompt_record.get_status_display(),
        'video_url': prompt_record.video_url or '',
        'video_file_url': prompt_record.video_file.url if prompt_record.video_file else '',
        'error_message': prompt_record.error_message or '',
        'thumbnail_url': (prompt_record.thumbnail.url if prompt_record.thumbnail else '') if prompt_record.status == 'completed' else '',
        'created_at': prompt_record.created_at.strftime('%d %b %Y, %H:%M'),
        'created_timestamp': int(prompt_record.created_at.timestamp() * 1000),
        'video_file_name': os.path.basename(prompt_record.video_file.name) if prompt_record.video_file else '',
        'stats': {
            'upload_date': prompt_record.upload_date or 'No disponible',
            'views': prompt_record.views_count,
            'likes': prompt_record.likes_count,
            'comments': prompt_record.comments_count,
            'uploader': prompt_record.uploader_name or 'Creador original',
            'duration': round(prompt_record.duration_seconds, 1) if prompt_record.duration_seconds else None,
        },
        'quota': {
            'is_unlimited': (request.user.role == 'superadmin' or request.user.is_unlimited_prompts),
            'limit': request.user.daily_prompt_limit,
            'used_today': request.user.get_prompts_used_today(),
            'remaining': request.user.get_prompts_remaining_today(),
            'can_generate': request.user.can_generate_prompt(),
        },
        'wp_post_url': prompt_record.wp_post_url or '',
        'wp_post_id': prompt_record.wp_post_id,
        'wp_article_title': prompt_record.wp_article_title or '',
        'wp_site_name': prompt_record.wp_site.name if prompt_record.wp_site else '',
        'wp_category': prompt_record.wp_category or 'Entretenimiento',
        'prompt_data': None,
    }
    
    if prompt_record.status == 'completed' and prompt_record.generated_prompt:
        try:
            parsed = json.loads(prompt_record.generated_prompt)
            if isinstance(parsed, dict) and 'full_prompt_markdown' in parsed:
                parsed['full_prompt_markdown'] = normalize_markdown_formatting(parsed['full_prompt_markdown'])
            data['prompt_data'] = parsed
        except json.JSONDecodeError:
            data['prompt_data'] = {'full_prompt_markdown': normalize_markdown_formatting(prompt_record.generated_prompt)}
            
    return JsonResponse(data)


@login_required
def batch_status_ajax(request):
    """
    Polling por lote para consultar el estado de múltiples prompts en un solo viaje de red.
    Acepta IDs por query parameter (?ids=1,2,3) o por POST.
    """
    raw_ids = request.GET.get('ids') or request.POST.get('ids', '')
    if not raw_ids:
        return JsonResponse({'status': 'success', 'items': []})

    import re
    try:
        if isinstance(raw_ids, str):
            id_list = [int(x.strip()) for x in re.split(r'[,]+', raw_ids) if x.strip().isdigit()]
        elif isinstance(raw_ids, list):
            id_list = [int(x) for x in raw_ids if str(x).isdigit()]
        else:
            id_list = []
    except (ValueError, TypeError):
        id_list = []

    if not id_list:
        return JsonResponse({'status': 'success', 'items': []})

    qs = VideoPrompt.objects.filter(id__in=id_list)
    if request.user.role != 'superadmin':
        qs = qs.filter(user=request.user)

    items = []
    for prompt_record in qs:
        prompt_data = None
        if prompt_record.status == 'completed' and prompt_record.generated_prompt:
            try:
                parsed = json.loads(prompt_record.generated_prompt)
                if isinstance(parsed, dict) and 'full_prompt_markdown' in parsed:
                    parsed['full_prompt_markdown'] = normalize_markdown_formatting(parsed['full_prompt_markdown'])
                prompt_data = parsed
            except json.JSONDecodeError:
                prompt_data = {'full_prompt_markdown': normalize_markdown_formatting(prompt_record.generated_prompt)}

        items.append({
            'id': prompt_record.id,
            'status': prompt_record.status,
            'status_display': prompt_record.get_status_display(),
            'video_url': prompt_record.video_url or '',
            'video_file_name': os.path.basename(prompt_record.video_file.name) if prompt_record.video_file else '',
            'video_file_url': prompt_record.video_file.url if prompt_record.video_file else '',
            'error_message': prompt_record.error_message or '',
            'thumbnail_url': (prompt_record.thumbnail.url if prompt_record.thumbnail else '') if prompt_record.status == 'completed' else '',
            'created_at': prompt_record.created_at.strftime('%d %b %Y, %H:%M'),
            'created_timestamp': int(prompt_record.created_at.timestamp() * 1000),
            'stats': {
                'upload_date': prompt_record.upload_date or 'No disponible',
                'views': prompt_record.views_count,
                'likes': prompt_record.likes_count,
                'comments': prompt_record.comments_count,
                'uploader': prompt_record.uploader_name or 'Creador original',
                'duration': round(prompt_record.duration_seconds, 1) if prompt_record.duration_seconds else None,
            },
            'prompt_data': prompt_data,
        })

    return JsonResponse({
        'status': 'success',
        'items': items,
        'quota': {
            'is_unlimited': (request.user.role == 'superadmin' or getattr(request.user, 'is_unlimited_prompts', False)),
            'limit': request.user.daily_prompt_limit,
            'used_today': request.user.get_prompts_used_today(),
            'remaining': request.user.get_prompts_remaining_today(),
            'can_generate': request.user.can_generate_prompt(),
        }
    })


@login_required
@require_POST
def delete_prompt_ajax(request, pk):
    """
    Elimina un prompt y sus archivos asociados.
    """
    if request.user.role == 'superadmin':
        prompt_record = get_object_or_404(VideoPrompt, pk=pk)
    else:
        prompt_record = get_object_or_404(VideoPrompt, pk=pk, user=request.user)
        
    # Eliminar thumbnail si existe
    if prompt_record.thumbnail and os.path.exists(prompt_record.thumbnail.path):
        try:
            os.remove(prompt_record.thumbnail.path)
        except Exception:
            pass
            
    # Eliminar video local si existe
    if prompt_record.video_file and os.path.exists(prompt_record.video_file.path):
        try:
            os.remove(prompt_record.video_file.path)
        except Exception:
            pass
            
    prompt_record.delete()
    return JsonResponse({'status': 'success', 'message': 'Registro eliminado correctamente.'})


@login_required
@require_POST
def generate_wp_post_ajax(request, pk):
    """
    Genera un artículo de blog para WordPress on-demand a partir del análisis
    cinematográfico guardado en VideoPrompt.
    """
    if request.user.role == 'superadmin':
        prompt_record = get_object_or_404(VideoPrompt, pk=pk)
    else:
        prompt_record = get_object_or_404(VideoPrompt, pk=pk, user=request.user)

    if prompt_record.status != 'completed':
        return JsonResponse({
            'success': False,
            'error': 'El prompt aún no ha finalizado su análisis cinematográfico.'
        }, status=400)

    force_regenerate = request.POST.get('regenerate') == 'true'

    try:
        from .services.wp_article_service import generate_and_publish_wp_article
        panel_url = request.build_absolute_uri('/')[:-1]
        result = generate_and_publish_wp_article(
            prompt_record,
            force_regenerate=force_regenerate,
            panel_url=panel_url
        )
        return JsonResponse({
            'success': True,
            'id': prompt_record.id,
            'wp_post_url': result.get('post_url') or '',
            'wp_post_id': result.get('post_id'),
            'wp_site_name': result.get('site_name') or '',
            'wp_article_title': result.get('article_title') or '',
            'wp_category': result.get('category') or 'Entretenimiento',
            'from_cache': result.get('from_cache', False),
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f"Error al generar artículo para WordPress: {str(e)}"
        }, status=400)


