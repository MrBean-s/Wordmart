import json, random, uuid
from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse, HttpResponseBadRequest
from .forms import *
from .models import *
from django.contrib import messages
from django.db.models import ProtectedError, Q, Count, Prefetch
from django.core.paginator import Paginator
from django.core.exceptions import BadRequest
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from collections import defaultdict, Counter
from django.urls import reverse
from itertools import groupby
from django.conf import settings
from datetime import datetime, timedelta, UTC
from django.utils import timezone as django_tz
from django.utils.formats import date_format
from dateutil.relativedelta import relativedelta, MO
from django.views.decorators.http import require_POST, require_GET
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache

@never_cache
def start_screen(request):
   languages = Language.objects.select_related('image').filter(in_user_set=True).all()

   return render(request, "start_page.html", {
      "languages": languages,
   })


def language_form(request, lang_id=None):

   can_edit_img = request.GET.get('can_edit_img', 'false')
   can_edit_img = can_edit_img.lower() == 'true'

   language = get_object_or_404(Language, pk=lang_id) if lang_id else None
   
   if request.method == 'POST':
      lang_form = LanguageForm(request.POST, request.FILES, instance=language)
               
      if lang_form.is_valid():
         new_lang = lang_form.save()
         if not language:
            new_lang.added_manually=True
            new_lang.save()

         if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(headers={'HX-Redirect': '/languages/'})
         return redirect('languages')
   else:
      countries = language.countries.all().order_by('name') if language else Country.objects.none()
      lang_form = LanguageForm(instance=language, initial={'countries': countries})

   return render(request, "forms/_language_form.html", 
      {
         "form": lang_form,
         "is_edit" : language is not None,
         'edit_lang_id': language.id if language else 0,
         'can_edit_img': can_edit_img
      })

@never_cache
def languages(request):
   languages = Language.objects.select_related('image').all()
   in_home_screen = Language.objects.filter(in_user_set=True).values_list('id', 'name')
   return render(request, "languages.html", {"languages": languages, "in_home_screen": in_home_screen})

def delete_language(request, lang_id):
   instance = get_object_or_404(Language, pk=lang_id)

   if request.method == 'POST':
      img = instance.image
      try:
         instance.delete()	
      except ProtectedError:
         messages.error(request, "Cannot delete language because it still has words. Delete the words first.")
      else:
         if img:
            storage = img.file.storage
            if storage.exists(img.file.name):
               storage.delete(img.file.name)
            img.delete()
         
         messages.success(request, "Language deleted.")

      return redirect('languages')

def add_lang_to_set(request):
   if request.method == 'POST':
      form = LanguageAddSetForm(request.POST)
      if form.is_valid():
         language = form.cleaned_data.get('language')
         language.in_user_set=True
         language.save()
         if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(headers={'HX-Redirect': '/'})
         return redirect('start_screen')
   else:
      form = LanguageAddSetForm()
   return render(request, 'forms/_add_language.html', {'form': form})

def add_lang_to_set_with_id(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)
   language.in_user_set=True
   language.save()

   return redirect('languages')

def remove_lang_from_set(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)
   language.in_user_set=False
   language.save()
   return redirect('start_screen')


def generic_confirm_delete(request):
   name = request.GET.get('name', '')
   delete_url = request.GET.get('delete_url', '')
   obj_type = request.GET.get('type', 'item')

   return render(request, 'forms/_confirm_delete.html', {
      'name': name,
      'delete_url': delete_url,
      'type': obj_type,
   })


def dashboard(request, lang_id):
   return HttpResponse("<h1>Hello, World!</h1>", content_type="text/html")

@never_cache
def word(request, lang_id, word_id=None):
   language = get_object_or_404(
      Language.objects.prefetch_related('countries', 'parts_of_speech'),
      pk=lang_id
   )
   
   word = get_object_or_404(
      Word.objects
         .select_related('language')
         .prefetch_related('language__countries', 'definitions__country_tags'),
      pk=word_id
   ) if word_id else None

   country_qs = Country.objects.filter(languages__pk=language.id)
   def_selected_ctys = {}

   if word:
      def_selected_ctys = {
         defn.id: defn.get_selected_countries_ids()
         for defn in word.definitions.all()
      }

   if request.method == 'POST':
      word_form = WordForm(request.POST, instance=word)

      def_formset = DefinitionFormSet(
         request.POST,
         request.FILES,
         instance=word,
         prefix='definitions',
      )
      # Build example formsets from POST data
      example_formsets = []
      total_defs = int(request.POST.get('definitions-TOTAL_FORMS', 0))
      for i in range(total_defs):
         prefix = f'def-{i}-examples'

         def_id = request.POST.get(f'definitions-{i}-id')
         def_instance = Definition.objects.get(pk=def_id) if def_id else None
         ex_fs = ExampleFormSet(request.POST, prefix=prefix, instance=def_instance, language=language)
         example_formsets.append(ex_fs)

      if (word_form.is_valid() and def_formset.is_valid() and
         all(ex_fs.is_valid() for ex_fs in example_formsets)):
         
         word = word_form.save(commit=False)
         word.language = language
         word.save()

         for idx, def_form in enumerate(def_formset.forms):
            # Skip forms marked for deletion
            if def_form.cleaned_data.get('DELETE', False):
               continue
            definition = def_form.save(commit=False)
            definition.word = word
            definition.save()

            country_set = request.POST.getlist(f'def-{idx}-countries')

            if '-999' in country_set: # -999: ALL tag
               definition.country_tags.set(language.countries.all());
               definition.save()
            else:
               countries = Country.objects.filter(id__in=country_set)
               definition.country_tags.set(countries)

            # original index to pick the correct example formset
            ex_fs = example_formsets[idx]
            examples = ex_fs.save(commit=False)
            for ex in examples:
               ex.definition = definition
               ex.status = 'P' if not ex.part_of_speech else 'C'
               ex.save()
            for obj in ex_fs.deleted_objects:
               obj.delete()
   
         # Handle deleted definitions
         for idx, def_form in enumerate(def_formset.forms):
            if def_form.cleaned_data.get('DELETE', False) and def_form.instance.pk:
               def_form.instance.delete()

         if not word.definitions.all():
            messages.success(request, "Word added to pending list")
            word.is_draft = True
         else:
            word.is_draft = False
         word.save()
   
         return redirect('word_details', lang_id=lang_id, word_id=word.id)
   else:
      word_form = WordForm(instance=word)
      def_formset = DefinitionFormSet(instance=word, prefix='definitions')
      for def_form in def_formset:
         if def_form.instance.pk:
            def_form.selected_country_ids = def_selected_ctys.get(def_form.instance.pk, [])
         else:
            def_form.selected_country_ids = []

      example_formsets = []

      for i, def_form in enumerate(def_formset):
         prefix = f'def-{i}-examples'
         def_instance = def_form.instance if def_form.instance.pk else None
         ex_fs = ExampleFormSet(prefix=prefix, instance=def_instance, language=language)


         example_formsets.append(ex_fs)
   
   return render(request, 'word_add_edit.html', {
      'word_form': word_form,
      'def_formset': def_formset,
      'example_formsets': example_formsets,
      'has_word': word is not None,
      'word_id': word_id,
      'lang_id': lang_id,
      'country_qs': country_qs,
   })

def word_delete(request, lang_id, word_id):
   word = get_object_or_404(
      Word.objects.prefetch_related('definitions'),
      pk=word_id,
      language_id=lang_id
   )

   word.delete()
   messages.success(request, "Word deleted")

   return redirect('word_list', lang_id=lang_id)


@require_GET
def verify_word_availability(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)
   query = request.GET.get('query', '')
   if not query:
      return JsonResponse({'error': 'Bad Request'},status=400)
   exists = Word.objects.filter(name=query).exists()

   return JsonResponse({'exists': exists}, status=200)

@never_cache
def word_list(request, lang_id):
   lang = get_object_or_404(Language, pk=lang_id)
   words_qs = lang.word_set.filter(is_draft=False)

   form = WordListFilters(request.GET, lang_id=lang_id)
   context = { 'lang_id': lang_id, 'form': form }

   # for dynamic prefetch
   def_qs = Definition.objects.prefetch_related('country_tags')
   ex_qs = Example.objects.select_related(
      'citation__source', 
      'citation__episode', 
      'citation__segment',
      'part_of_speech',
   )

   if form.is_valid():
      time_unit_type = form.cleaned_data.get('timeUnitType')
      time_unit_value = form.cleaned_data.get('timeUnitValue')
      forgetting_frequency = form.cleaned_data.get('forgettingFrequency')
      region = form.cleaned_data.get('region')
      source = form.cleaned_data.get('source')
   
      if time_unit_type in ['D', 'W', 'M', 'Y'] and time_unit_value:
         now = django_tz.localtime()

         if time_unit_type == 'M':
            target = now - relativedelta(months=time_unit_value)
            start_date = target.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end_date = start_date + relativedelta(months=1) - timedelta(microseconds=1)

         elif time_unit_type == 'Y':
            target = now - relativedelta(years=time_unit_value)
            start_date = target.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end_date = start_date + relativedelta(years=1) - timedelta(microseconds=1)

         elif time_unit_type == 'W':
            target = now - relativedelta(weeks=time_unit_value)
            start_date = (target + relativedelta(weekday=MO(-1))).replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = start_date + relativedelta(weeks=1) - timedelta(microseconds=1)

         elif time_unit_type == 'D':
            target = now - relativedelta(days=time_unit_value)
            start_date = target.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = target.replace(hour=23, minute=59, second=59, microsecond=999999)
         
         context.update({'start_date': start_date, 'end_date': end_date})

         words_qs = words_qs.filter(added_at__range=(start_date, end_date))

      if forgetting_frequency:
         words_qs = words_qs.filter(definitions__forgetting_frequency=forgetting_frequency)
         def_qs = def_qs.filter(forgetting_frequency=forgetting_frequency)
      
      if region:
         qualifying_def_ids = Definition.objects.annotate(
            total_tags=Count('country_tags')
         ).filter(
            total_tags=1,
            country_tags=region
         ).values_list('pk', flat=True)

         words_qs = words_qs.filter(definitions__pk__in=qualifying_def_ids)
         def_qs = def_qs.filter(pk__in=qualifying_def_ids)
      if source:
         source_q = (
            Q(citation__source_id=source) |
            Q(citation__episode__source_id=source) |
            Q(citation__segment__source_id=source)
         )
         matching_examples = Example.objects.filter(source_q)

         words_qs = words_qs.filter(definitions__examples__in=matching_examples)
         def_qs = def_qs.filter(examples__in=matching_examples).distinct()
         ex_qs = ex_qs.filter(source_q)

   def_prefetch = Prefetch(
      'definitions',
      queryset=def_qs.prefetch_related(Prefetch('examples', queryset=ex_qs))
   )

   words_qs = words_qs.distinct().prefetch_related(def_prefetch).order_by('-id')

   paginator = Paginator(words_qs, 30)
   page_obj = paginator.get_page(request.GET.get('page', 1))
   page_range = paginator.get_elided_page_range(
      number=page_obj.number,
      on_each_side=2,
      on_ends=1
   )

   context.update({'page_obj': page_obj, 'pages': page_range})

   return render(request, 'word_list.html', context)


@csrf_exempt
def import_words_from_old_json(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)
   if request.method != 'POST':
      return JsonResponse({'error': 'POST only'}, status=405)
   try:
      data = json.loads(request.body)
   except json.JSONDecodeError:
      return JsonResponse({'error': 'Invalid JSON'}, status=400)
   if not isinstance(data, list):
      return JsonResponse({'error': 'Expecting list of words'}, status=400)

   created_word_ids = []
   errors = []

   try:
      with transaction.atomic():
         for word_data in data:
            name = word_data.get('name')
            if not name:
               errors.append(f'Missing name in {word_data}')
               raise ValueError('Missing name')

            word = Word.objects.create(name=name, language=language)

            for def_data in word_data.get('definitions', []):
               desc = def_data.get('definition')  # old field name
               if not desc:
                     errors.append(f'Missing definition for word "{name}"')
                     raise ValueError('Missing definition')

               definition = Definition.objects.create(
                     word=word,
                     description=desc
               )

               for ex_data in def_data.get('examples', []):
                     ex_text = ex_data.get('example')
                     if not ex_text:
                        errors.append(f'Missing example for word "{name}"')
                        raise ValueError('Missing example')

                     Example.objects.create(
                        definition=definition,
                        description=ex_text
                     )

            created_word_ids.append(word.id)
   except ValueError:
      pass

   if errors:
      return JsonResponse({'status': 'failed', 'errors': errors}, status=400)

   return JsonResponse({'status': 'ok', 'created_word_ids': created_word_ids}, status=201)

def search(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)
   
   if not request.META.get('HTTP_HX_REQUEST'):
      return redirect('word_list', lang_id=lang_id)

   form = ModalSearchForm(request.GET or None, initial={'filter': ['word']})
   filters = []
   results = {}
   is_submission = False

   if form.is_valid():
      is_submission = True
      query = form.cleaned_data['query'].lower()
      filters = form.cleaned_data['filter']
      temp_limit = form.cleaned_data.get('limit')
      limit = 5 if not temp_limit or not(0 < temp_limit <= 30) else temp_limit
      newest_first = form.cleaned_data['newest_first']
      order = 'id' if not newest_first else '-id'
   else:
      return render(request, 'modals/simple_search.html', {
      'lang_id': lang_id,
      'search_form': form,
      'results_dict': results
      })
      
   if 'word' in filters:
      word_results = Word.objects.filter(
         name__icontains=query, language_id=lang_id
      ).values_list('id', 'name').order_by(order)[:limit]

      for wid, wname in word_results.iterator():
         results.setdefault('words', []).append({'word_id': wid, 'word': wname})

   if 'definition' in filters:
      desc_results = Definition.objects.filter(
         description__icontains=query, word__language_id=lang_id
      ).values_list('description', 'word__name', 'word__id').order_by(order)[:limit]

      for desc, wname, wid in desc_results.iterator():
         results.setdefault('definitions', []).append({
            'word_id': wid, 'definition': desc, 'word': wname,
         })
   
   if 'example' in filters:
      ex_results = Example.objects.filter(
         description__icontains=query, definition__word__language_id=lang_id
      ).values_list(
         'description', 'definition__word__name', 'definition__word__id'
      ).order_by(order)[:limit]
      
      temp = defaultdict(list)
      for desc, wname, wid in ex_results:
         temp[(wname, wid)].append(desc)

      example_results = [
         { 'word': word, 'word_id': word_id, 'examples': examples }
         for (word, word_id), examples in temp.items()
      ]

      if example_results:
         results["examples"] = example_results

   return render(request, 'modals/simple_search.html', {
      'lang_id': lang_id,
      'search_form': form,
      'results_dict': results,
      'is_submission': is_submission,
      'has_results': len(results) > 0 
   })
   
@never_cache
def word_details(request, lang_id, word_id):
   language = get_object_or_404(
      Language.objects.prefetch_related('countries', 'parts_of_speech'),
      pk=lang_id
   )
   
   word = get_object_or_404(
      Word.objects.prefetch_related('definitions__country_tags'),
      pk=word_id
   )

   rels = WordRelation.objects.filter(word_1=word).select_related('word_2')
   related_words = [
      { "id": rel.word_2.id, "name": rel.word_2.name, "relation": rel.get_relation_type_display().title() }
      for rel in rels
   ]
   
   all_lang_countries_ids = {c.id for c in language.countries.all()}
   context = { 
      "name": word.name,
      "id": word.id,
      "definitions": [
         defn.to_json(all_lang_countries_ids=all_lang_countries_ids)
         for defn in word.definitions.all()
      ]
   }

   return render(request, 'word_details.html', {
      'lang_id': lang_id,
      'ngram_code': language.iso_code,
      'context': context,
      'related_words': related_words,
      'category_map_json': Source.get_category_map(),
      'source_to_cat_json': dict(Source.objects.filter(language_id=lang_id).values_list('id', 'source_category'))
   })

@never_cache
def countries(request):
   if request.method == "POST":
      form = CountryForm(request.POST)
      if form.is_valid():
         country = form.save(commit=False)
         country.added_manually = True
         country.save()
         return redirect('countries')
   else:   
      form = CountryForm(instance=None)

   countries = Country.objects.all().order_by('name')

   return render(request, 'countries.html', {
      'form': form,
      'countries': countries
   })

def delete_country(request, cty_id):
   instance = get_object_or_404(Country, pk=cty_id)

   if request.method == 'POST':
      instance.delete()      
      messages.success(request, "Country deleted.")

      return redirect('countries')

def show_image(request, img_id):
   instance = get_object_or_404(Image, pk=img_id)

   return render(request, 'modals/_image_preview.html', {'image': instance})


def link_word(request, lang_id, word_id):
   word1 = get_object_or_404(Word, pk=word_id)
   ajax_url = reverse('search_word', kwargs={'lang_id': lang_id})

   if request.method == "POST":
      form = LinkWordForm(request.POST, ajax_url=ajax_url)

      if form.is_valid():
         word2 = form.cleaned_data['word_2']
         rel_type = form.cleaned_data['relation_type']


         if word2.language.pk == lang_id:
            WordRelation.objects.get_or_create(
               word_1=word1,
               word_2=word2,
               defaults={'relation_type': rel_type}
            )
            WordRelation.objects.get_or_create(
               word_1=word2,
               word_2=word1,
               defaults={'relation_type': rel_type}
            )

         if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(headers={'HX-Redirect': f'/lang/{lang_id}/word/{word_id}/'})
         return redirect('word_details', lang_id=lang_id, word_id=word_id)
   else:
      form = LinkWordForm(ajax_url=ajax_url)

   form.fields['word_1_id'].initial = word1.id


   return render(request, 'forms/_link_word_form.html', {
      "form": form,
      "lang_id": lang_id,
      "word_id": word_id
   })


def word_only_search(request, lang_id):
   query = request.GET.get('query', '')
   if query:
      word_qs = Word.objects.filter(
         name__icontains=query, language_id=lang_id
      ).values('id', 'name').order_by('name')[:15]

      results = {
         'results': [ {"id": item['id'], "text": item['name']} for item in word_qs]
      }
   return JsonResponse(results)


def link_word_edit(request, lang_id, word_id, rel_word_id):
   word = get_object_or_404(Word, pk=word_id)
   rel_word = get_object_or_404(Word, pk=rel_word_id)

   relations = WordRelation.objects.filter(
      (Q(word_1=word) & Q(word_2=rel_word)) |
      (Q(word_1=rel_word) & Q(word_2=word))
   )
   form = RelationTypeForm({'relation_type': relations.first().relation_type})

   if request.method == "POST":
      form = RelationTypeForm(request.POST)
      if form.is_valid():
         for rel in relations:
            rel.relation_type = form.cleaned_data['relation_type']
            rel.save()

         return redirect('word_details', lang_id=lang_id, word_id=word_id)

   return render(request, 'forms/_link_word_edit.html', {
      "lang_id": lang_id,
      'word_id': word_id,
      "rel_word": rel_word,
      'form': form
   })

def unlink_word(request, lang_id, word_id, rel_word_id):
   word = get_object_or_404(Word, pk=word_id)
   rel_word = get_object_or_404(Word, pk=rel_word_id)

   if request.method == "POST":
      relations = WordRelation.objects.filter(
         (Q(word_1=word) & Q(word_2=rel_word)) |
         (Q(word_1=rel_word) & Q(word_2=word))
      )
      relations.delete()
      return redirect('word_details', lang_id=lang_id, word_id=word_id)

   return render(request, 'forms/_unlink_word.html', {
      "lang_id": lang_id,
      "word": word,
      "rel_word": rel_word,
      "form_url": request.path
   })


@never_cache
def sources(request, lang_id):

   section_sources = Source.objects.filter(
      language_id=lang_id,
      source_category__in=['BOK', 'MOV', 'TVS', 'ALB']
   ).order_by('-source_category')

   CATEGORY_ORDER = ['TVS', 'MOV', 'BOK', 'ALB']
   
   #to preserve insertion order
   category_labels = dict(Source.SourceCategory.choices)
   sections = {category_labels[code]: [] for code in CATEGORY_ORDER if code in category_labels}

   for source in section_sources:
      label = source.get_source_category_display()
      if label in sections:
         sections[label].append(source)

   sections = {label: sources for label, sources in sections.items() if sources}

   audio_sources = Source.objects.filter(
      language_id=lang_id,
      source_category__in=['ABK', 'SON', 'POD']
   ).order_by('-source_category')

   return render(request, 'sources.html', {
      'lang_id': lang_id,
      'sections' : sections,
      'audio_sources': audio_sources
   })

def sources_add_edit(request, lang_id, source_id=None):

   source = get_object_or_404(Source, pk=source_id) if source_id else None
   language = get_object_or_404(Language, pk=lang_id)

   if request.method == "POST":
      form = SourceForm(request.POST, request.FILES, instance=source)
      if form.is_valid():
         source = form.save(commit=False)
         source.language = language
         source.save()

         action = 'edited' if source_id else 'created'
         messages.success(request, f"Source {action}")
         if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(headers={'HX-Refresh': 'true'})
         return redirect('sources', lang_id=lang_id)
   else:
      form = SourceForm(instance=source)
      
   return render(request, 'forms/_source_form.html', {
      "lang_id": lang_id,
      "source": source,
      "form": form,
      "is_edit": source_id is not None
   })


def source_delete(request, lang_id, source_id):
   source = get_object_or_404(Source, pk=source_id)

   if request.method == 'POST':
      try:
         source.delete()	
      except ProtectedError:
         messages.error(request, "Cannot delete Source cause it still has related data. Delete all episodes/content first.")
      else:         
         messages.success(request, "Source deleted.")

      return redirect('sources', lang_id=lang_id)


@never_cache
def episodes(request, lang_id, source_id):
   source = get_object_or_404(Source, pk=source_id)
   episodes = Episode.objects.filter(source_id=source_id).order_by('season_number')
   grouped_by_season = {season: list(group) for season, group in groupby(episodes, key=lambda x: x.season_number)}

   return render(request, 'episodes.html', {
      'lang_id': lang_id,
      'grouped_by_season': grouped_by_season,
      'source': source
   })


def episode_add_edit(request, source_id, episode_id=None):
   episode = get_object_or_404(Episode, pk=episode_id) if episode_id else None
   source = get_object_or_404(Source, pk=source_id)

   if request.method == "POST":
      if episode is None:
         episode = Episode(source=source)
      
      form = EpisodeForm(request.POST, instance=episode)
      if form.is_valid():
         episode = form.save(commit=False)
         episode.source = source
         episode.save()
         
         action = 'edited' if episode_id else 'created'
         messages.success(request, f"Episode {action}")
         
         if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(headers={'HX-Refresh': 'true'})
         return redirect('episodes', source_id=source_id)
   else:
      form = EpisodeForm(instance=episode)

   return render(request, 'forms/_episode_form.html', {
      'source_id': source_id,
      'episode_id': episode_id,
      'form': form,
      'is_edit': episode is not None,
      'episode': episode
   })


def episode_delete(request, source_id, episode_id):
   episode = get_object_or_404(Episode, pk=episode_id)
   source = get_object_or_404(Source, pk=source_id)
   try:
      episode.delete()
      messages.success(request, "Episode deleted.")
   except ProtectedError:
      messages.error(request, "Remove all citations before deleting the episode") 
   
   return redirect('episodes', lang_id=source.language_id, source_id=source_id)


@never_cache
def segments(request, lang_id, source_id):
   source = get_object_or_404(
      Source.objects.prefetch_related('segments'),
      pk=source_id
   )

   categorized = {}
   for seg in source.segments.all().order_by('-segment_type'):
      categorized.setdefault(seg.get_segment_type_display(), []).append({'id': seg.id, 'number': seg.number, 'name': seg.name})
   
   return render(request, 'segments.html', {
      'lang_id': lang_id,
      'source_id': source_id,
      'source': source,
      'segments': categorized
   })


def segment_add_edit(request, source_id, segment_id=None):
   source = get_object_or_404(Source, pk=source_id)
   segment = get_object_or_404(Segment, pk=segment_id) if segment_id else None
   if request.method == 'POST':
      form = SegmentForm(request.POST, instance=segment, source=source, source_category=source.source_category)
      if form.is_valid():
         form.save()
         action = 'edited' if segment_id else 'created'
         messages.success(request, f"Segment {action}")

         if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(headers={'HX-Refresh': 'true'})
         return redirect('episodes', source_id=source_id)
   else:
      form = SegmentForm(instance=segment, source=source, source_category=source.source_category)
   
   return render(request, 'forms/_segment_form.html', {
      'form': form,
      'source_id': source_id,
      'is_edit': segment is not None,
      'segment': segment,
   })


def segment_delete(request, source_id, segment_id):
   segment = get_object_or_404(Segment, pk=segment_id)
   source = get_object_or_404(Source, pk=source_id)
   try:
      segment.delete()
      messages.success(request, "Segment deleted.")
   except ProtectedError:
      messages.error(request, "Remove all citations before deleting the segment")
   
   return redirect('segments', lang_id=source.language_id, source_id=source_id)


def citation_delete(request, lang_id, citation_id):
   citation = get_object_or_404(Citation, pk=citation_id)
   
   if request.method == 'POST':
      form = CitationDelete(request.POST, citation_id=citation_id)
      if form.is_valid():
         form.save()
         if request.META.get('HTTP_HX_REQUEST'):
            if citation.episode:
               kind, obj_id = 'episode', citation.episode_id
            elif citation.segment:
               kind, obj_id = 'segment', citation.segment_id
            else:
               kind, obj_id = 'source', citation.source_id

            return HttpResponse(headers={'HX-Redirect': f'/lang/{lang_id}/{kind}/{obj_id}/play-session/'})
         return redirect('episodes', source_id=source_id)
   else:
      form = CitationDelete(citation_id=citation_id)

   return render(request, 'forms/_citation_delete.html', {
      'lang_id': lang_id,
      'citation_id': citation_id,
      'form': form
   })

@never_cache
def play_session(request, lang_id, source_id=None, episode_id=None, segment_id=None):
   source = get_object_or_404(Source, pk=source_id) if source_id else None
   episode = get_object_or_404(Episode, pk=episode_id) if episode_id else None
   segment = get_object_or_404(Segment, pk=segment_id) if segment_id else None
   
   if not any([source, episode, segment]):
      return HttpResponseBadRequest('At least one source is required')

   defn_ajax_url = reverse('get_definitions')

   qs = ( 
      Citation.objects
      .values_list(
         'id',
         'spotted_at',
         'example__definition__word_id',
         'example__definition__word__name',
         'example__definition_id',
         'example__definition__description',
         'example_id',
         'example__description',
         'image__file',
         'image_id',
         'page'
      )
   )

   obj = episode or segment or source
   temp_src = getattr(obj, 'source', obj)
   qs = qs.filter(**{('source' if obj == source else obj.__class__.__name__.lower()): obj})
   qs = qs.order_by('spotted_at', 'page')

   spreads = [] # for books only

   if temp_src.source_category in ['MOV', 'TVS']:
      template = 'play_session_film.html'
   elif temp_src.source_category in ['ALB', 'SON', 'ABK', 'POD']:
      template = 'play_session_sound.html'
   elif temp_src.source_category in ['BOK']:
      template = 'play_session_book.html'
      
      citations_per_page = 4
      first_left_page_count = 3

      flat_citations = list(qs)

      # First spread: title + 3 citations
      left_first = flat_citations[:first_left_page_count]
      right_first = flat_citations[first_left_page_count:first_left_page_count + citations_per_page]
      spreads.append({'left': left_first, 'right': right_first, 'chapter_title': segment.name,})


      remaining = flat_citations[first_left_page_count + citations_per_page:]

      for i in range(0, len(remaining), citations_per_page * 2):
         left_page = remaining[i:i + citations_per_page]
         right_page = remaining[i + citations_per_page:i + citations_per_page * 2]
         if left_page or right_page:
            spreads.append({
               'left': left_page or [],
               'right': right_page or [],
               'chapter_title': None,
            })
   
   # elif temp_src.source_category in ['GAM', 'OTH']
      # template = 'play_session_timeline.html'
   
   return render(request, template, {
      "lang_id": lang_id,
      "source": source,
      "episode": episode,
      "segment": segment,
      'defn_ajax_url': defn_ajax_url,
      'citations': qs,
      "MEDIA_URL": settings.MEDIA_URL,
      'spreads': spreads if spreads else None
   })


def play_session_cite(request, lang_id, source_id=None, episode_id=None, segment_id=None, citation_id=None):
   source = get_object_or_404(Source, pk=source_id) if source_id else None
   episode = get_object_or_404(Episode, pk=episode_id) if episode_id else None
   segment = get_object_or_404(Segment, pk=segment_id) if segment_id else None
   citation = get_object_or_404(Citation, pk=citation_id) if citation_id else None

   ajax_url = reverse('search_word', kwargs={'lang_id': lang_id})

   if not any([source, episode, segment]):
      return HttpResponseBadRequest('At least one source is required')
   
   can_add_img = bool(episode or (source and source.source_category == 'MOV'))
   is_book = bool(segment and segment.source.source_category == 'BOK')
   
   initial_data = { 
      'spotted_at': citation.spotted_at,
      'example': citation.example.description,
      'page': citation.page,
      'pending_definition': citation.example.definition.description == '',
      'definition_select': citation.example.definition_id if citation.example.definition.description != '' else None
   } if citation else {}
   
   if request.method == "POST":
      form = CitationForm(request.POST, request.FILES, ajax_url=ajax_url, initial=initial_data, can_add_img=can_add_img, is_book=is_book)
      if form.is_valid():
         word_val = form.cleaned_data['word']
         new_def = form.cleaned_data['definition_input']
         is_pending_def = form.cleaned_data['pending_definition']
         existing_def_id = form.cleaned_data['definition_select']

         if word_val.isdigit():
            word = get_object_or_404(Word, pk=int(word_val))
            word.is_draft = False; word.save()
         else:
            word, created = Word.objects.get_or_create(name=word_val, language_id=lang_id)
         
         if existing_def_id and existing_def_id.isdigit() and existing_def_id != '-1':
            definition = get_object_or_404(Definition, pk=int(existing_def_id))
         elif new_def or is_pending_def:
            definition = Definition.objects.create(description=new_def, word=word)

         definition.status = 'P' if is_pending_def else 'C'
         definition.save()
         
         initial_word_id = citation.example.definition.word_id if citation else None
         initial_word = citation.example.definition.word if citation else None

         example = citation.example if citation else Example()
         example.description = form.cleaned_data['example']
         example.definition = definition
         example.save()

         image_file = form.cleaned_data.get('image_file')
         delete_image = form.data.get("citation-image-DELETE")
         spotted_at = form.cleaned_data['spotted_at']

         if not citation:
            citation = Citation.objects.create(
               spotted_at=spotted_at,
               example=example,
               source=source,
               episode=episode,
               segment=segment
            )
         else:
            citation.spotted_at=spotted_at
            if image_file:
               old_img = citation.image
               if old_img and image_file:
                  storage = old_img.file.storage
                  if storage.exists(old_img.file.name):
                     storage.delete(old_img.file.name)
                  old_img.delete()
            elif delete_image and delete_image != 'false' and citation.image:
               img_to_del = citation.image
               storage = img_to_del.file.storage
               if storage.exists(img_to_del.file.name):
                  storage.delete(img_to_del.file.name)
               img_to_del.delete()
               citation.image = None

            # If theres any pending word in the citation edit panel, the definition input is the only one shown
            # This creates a new def every time so its necessary to delete the 'Pending' orphans
            if initial_word_id == word.id:
               word_to_delete_from = word
            else:
               word_to_delete_from = initial_word
            
            pending_def = word_to_delete_from.definitions.filter(status='P', description='', examples__isnull=True).first()

            if pending_def:
               pending_def.delete()

         if image_file:
            citation.image = Image.objects.create(file=image_file)

         page = form.cleaned_data.get('page')
         citation.page = page
         citation.save()

         if source:
            redirect_url = reverse('play_session_source', kwargs={'lang_id': lang_id, 'source_id': source_id})
         
         elif episode:
            redirect_url = reverse('play_session_episode', kwargs={'lang_id': lang_id, 'episode_id': episode_id})
         
         elif segment:
            redirect_url = reverse('play_session_segment', kwargs={'lang_id': lang_id, 'segment_id': segment_id})

         if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(headers={'HX-Redirect': redirect_url})
         return redirect(redirect_url)
      else:
         print(form.errors)
         
   else:      
      if citation:
         ex = citation.example
         def_choices = [(d.id, d.description) for d in ex.definition.word.definitions.all()]
         form = CitationForm(
            ajax_url=ajax_url,
            initial=initial_data,
            initial_word_id=ex.definition.word_id,
            initial_word=ex.definition.word.name,
            can_add_img=can_add_img,
            is_book=is_book,
            existing_img_path=citation.image.file.url if citation.image and citation.image.file else '',
            definition_choices=def_choices
         )
      else:
         form = CitationForm(ajax_url=ajax_url, can_add_img=can_add_img, is_book=is_book)

   return render(request, 'forms/_citation_form.html', {
      'lang_id': lang_id,
      'source_id': source_id,
      'episode_id': episode_id,
      'segment_id': segment_id,
      'form': form,
      'path': request.path,
      'is_edit': citation is not None,
      'citation_id': citation_id
   })


def get_definitions(request):
   word_id = request.GET.get('word_id')
   if not word_id:
      return HttpResponseBadRequest('word_id is missing')

   if not word_id.isdigit():
      return JsonResponse({'results': []})

   word = get_object_or_404(
      Word.objects.prefetch_related('definitions'),
      pk=word_id
   )

   results = {
      'results': [ {'id': defn.id, 'text': defn.description} for defn in word.definitions.exclude(description='') ]
   }

   return JsonResponse(results)

def search_episodes_or_segments(request, source_id):
   search_episodes = json.loads(request.GET.get('search_episodes', 'false').lower())
   
   source = get_object_or_404(
      Source.objects.prefetch_related('episodes', 'segments'),
      pk=source_id
   )   

   if search_episodes:
      results = { 'results': [ { 'value': ep.id, 'text': str(ep) } for ep in source.episodes.all().order_by('-season_number', '-episode_number') ] }
   else:
      results = { 'results': [ { 'value': seg.id, 'text': str(seg) } for seg in source.segments.all().order_by('number') ] }

   return JsonResponse(results)


def search_source(request, lang_id):
   query = request.GET.get('query', '')
   
   if not query:
      return JsonResponse({"results": []})

   categories = dict(Source.SourceCategory.choices)
   by_category = defaultdict(list)

   for src in Source.objects.filter(language_id=lang_id, name__icontains=query).values('id', 'name', 'source_category'):
      cat = src['source_category']
      url = reverse(Source.REDIRECT_URLS[cat], kwargs={'lang_id': lang_id, 'source_id': src['id']})
      by_category[categories.get(cat)].append({'id': src['id'], 'text': src['name'], 'redirect_url': url})
   
   results = [ {"text": cat, "children": items} for cat, items in by_category.items() ]

   return JsonResponse({"results": results})


def example_cite(request, lang_id, example_id):

   example = get_object_or_404(Example, pk=example_id)

   if request.method == 'POST':
      form = CitationFormDetailsPage(request.POST, request.FILES, lang_id=lang_id)
      
      if form.is_valid():
         source = form.cleaned_data['source']
         spotted_at = form.cleaned_data['spotted_at']
         episode_or_segment_id = form.cleaned_data.get('episode_or_segment')
         
         structure_type = source.get_structure_type()

         if structure_type in ('episodes', 'segments') and not episode_or_segment_id:
            form.add_error('episode_or_segment', 'This field is required for the selected source.')
            return render(request, 'forms/_citation_form_details_page.html', {
               'form': form, 'lang_id': lang_id, 'example_id': example_id
            })
            
         instance_id = int(episode_or_segment_id) if episode_or_segment_id else None

         if structure_type == 'episodes':
            episode = get_object_or_404(Episode, pk=instance_id)
            citation = Citation.objects.create(
               spotted_at=spotted_at,
               example=example,
               source=None,
               episode=episode,
            )
            redirect_url = reverse('play_session_episode', kwargs={'lang_id': lang_id, 'episode_id': episode.id})
         elif structure_type == 'segments':
            segment = get_object_or_404(Segment, pk=instance_id)
            citation = Citation.objects.create(
               spotted_at=spotted_at,
               example=example,
               source=None,
               segment=segment,
            )
            redirect_url = reverse('play_session_segment', kwargs={'lang_id': lang_id, 'segment_id': segment.id})
         else:
            citation = Citation.objects.create(
               spotted_at=spotted_at,
               example=example,
               source=source
            )
            redirect_url = reverse('play_session_source', kwargs={'lang_id': lang_id, 'source_id': source.id})     

         image_file = form.cleaned_data.get('image_file')
         if image_file:
            citation.image = Image.objects.create(file=image_file)
            citation.save()

         if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(headers={'HX-Redirect': redirect_url})
         return redirect(redirect_url)
   else:
      form = CitationFormDetailsPage(lang_id=lang_id)

   return render(request, 'forms/_citation_form_details_page.html', {
      'form': form,
      'lang_id': lang_id,
      'example_id': example_id
   })


@never_cache
def word_list_pending(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)

   words_with_missing_fields = (
      # will replace these Q()s with status == 'P' when I had fixed my own words
      Word.objects.filter(
         Q(definitions__isnull=True) |
         Q(definitions__description__isnull=True) | Q(definitions__description='') |
         Q(definitions__examples__isnull=True) |
         Q(definitions__examples__part_of_speech__isnull=True),
         # Q(definitions__status='P') |
         # Q(definitions__examples__status='P')
         language_id=lang_id,
         is_draft=False
      )
      .prefetch_related(
         'definitions',
         'definitions__examples',
         'definitions__examples__part_of_speech'
      )
      .distinct()
   ).order_by('-added_at')

   paginator = Paginator(words_with_missing_fields, 30)
   page_obj = paginator.get_page(request.GET.get('page', 1))
   word_results = []

   for w in page_obj:
      def_counts = Counter()
      ex_counts = Counter()
      
      defs = w.definitions.all()
      
      if not defs:
         def_counts['definitions'] += 1

      for d in defs:
         if not d.description or d.description == 'Pending':
            def_counts['description'] += 1

         examples = d.examples.all()
         if not examples:
            ex_counts['examples'] += 1
         else:
            for e in examples:
               if not e.part_of_speech:
                  ex_counts['parts of speech'] +=1

      word_results.append({
         'id': w.id,
         'name': w.name,
         'added_at': w.added_at,
         'missing_counts': {
            'definitions': dict(def_counts),
            'examples': dict(ex_counts)
         }
      })
   
   page_obj.object_list = word_results

   page_range = paginator.get_elided_page_range(
      number=page_obj.number,
      on_each_side=2,
      on_ends=1
   )

   drafts = Word.objects.filter(is_draft=True)
   return render(request, 'word_list_pending.html', {
      'page_obj': page_obj,
      'pages': page_range,
      'lang_id': lang_id,
      'drafts': drafts
   })


def add_word_later(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)
   if request.method == "POST":
      form = SearchLater(request.POST, lang_id=lang_id)
      if form.is_valid():
         form.save()
         if request.META.get('HTTP_HX_REQUEST'):
            return HttpResponse(headers={'HX-Redirect': reverse('word_list_pending', kwargs={'lang_id': lang_id})})
         return redirect('word_list_pending', lang_id=lang_id)
   else:
      form = SearchLater(lang_id=lang_id)
   return render(request, 'forms/_search_later_form.html', { 'lang_id': lang_id, 'form': form } )


@never_cache
def deck(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)

   active_quizzes = [
      {
         'uuid': uuid,
         'creation_date': django_tz.localtime(datetime.fromisoformat(data['creation_date'])),
         'quiz_type': data['quiz_type'],
         'used_settings': data['used_settings']
      } for uuid, data in (request.session.get('quizzes') or {}).items()
   ]

   decks = Deck.objects.prefetch_related('questions').filter(language=language).all()

   return render(request, 'decks.html', {
      'lang_id': lang_id,
      'iso_code': language.iso_code,
      'decks': decks,
      'active_quizzes': active_quizzes,
   })


def quiz_settings(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)
   form = QuizSettingsForm(request.POST or None, lang_id=lang_id)

   if request.method == 'POST' and form.is_valid():
      quiz_type = form.cleaned_data.get('quiz_type')
      source = form.cleaned_data.get('source')
      forgetting_frequency = form.cleaned_data.get('forgettingFrequency')
      prioritize = form.cleaned_data.get('prioritize', 'ANY')
      requested_quantity = form.cleaned_data.get('quantity', 10 if quiz_type == 'WW' else 5)
      distractors_qty = 3
      options_per_question = distractors_qty + 1
      use_timer = form.cleaned_data.get('use_timer', False)
      time_per_question = form.cleaned_data.get('time_per_question', 20)
      used_settings = { 'quantity': requested_quantity }
      if use_timer:
         used_settings.update({'Time/Question': str(time_per_question) + 's' })
      
      definitions_qs = Definition.objects.filter(
         word__language_id=lang_id
      ).exclude(word__is_draft=True)

      if source:
         definitions_qs = definitions_qs.filter(
            Q(examples__citation__source=source) |
            Q(examples__citation__episode__source=source) |
            Q(examples__citation__segment__source=source)
         ).distinct()
         used_settings['source'] = source.name

      if forgetting_frequency:
         definitions_qs = definitions_qs.filter(forgetting_frequency=forgetting_frequency)
         used_settings['forgetting_frequency'] = forgetting_frequency

      total_available = definitions_qs.count()
      needed = requested_quantity * options_per_question if quiz_type == 'MO' else requested_quantity

      if total_available < needed:
         form.add_error(
            None,
            f"Not enough words. Need {needed} but only {total_available} available. Remove filters, change the mode, or reduce quantity."
         )
         return render(request, 'forms/_quiz_settings_form.html', {'lang_id': lang_id, 'form': form})
      
      quantity = min(needed, total_available)

      def weighted_sample_unique(population, weights, k):
         if k > len(population):
            raise ValueError("k cannot be larger than the population")

         chosen = []
         while len(chosen) < k:
            candidate = random.choices(population, weights=weights, k=1)[0]
            if candidate not in chosen:
               chosen.append(candidate)
         return chosen
      
      if prioritize == 'OLDEST':
         definitions = list(definitions_qs.order_by('word__added_at'))
         weights = [1 / (i + 1) for i in range(len(definitions))]
         selected = weighted_sample_unique(definitions, weights, quantity)
         used_settings['prioritize'] = 'OLDEST'
      elif prioritize == 'NEWEST':
         definitions = list(definitions_qs.order_by('-word__added_at'))
         weights = [1 / (i + 1) for i in range(len(definitions))]
         selected = weighted_sample_unique(definitions, weights, quantity)
         used_settings['prioritize'] = 'NEWEST'
      else:
         definitions = list(definitions_qs)
         selected = random.sample(definitions, quantity)
         used_settings['prioritize'] = 'ANY'
   
      questions = []
      answer_key = {}
      definition_and_distractors = {}
      used_definitions = []
      if quiz_type == 'MO':
         for question_idx, i in enumerate(range(0, len(selected), options_per_question)):
            group = selected[i:i + options_per_question]
            correct_def = group[0]
            definition_and_distractors[correct_def.id] =  [defn.word.id for defn in group[1:]]
            correct_word = correct_def.word

            option_words = [d.word.name for d in group]
            random.shuffle(option_words)
            
            questions.append({
               'type': 'radiogroup',
               'name': f'q{question_idx}',
               'title': correct_def.description,
               'choices': option_words
            })

            answer_key[f'q{question_idx}'] = { 'word_id': correct_word.id, 'word_name': correct_word.name, 'defn_id': correct_def.id }
      elif quiz_type == 'WW':
         for i, defn in enumerate(selected):
            questions.append({
               'type': 'text',
               'name': f'q{i}',
               'title': defn.description,
            })
            answer_key[f'q{i}'] = { 'word_id': defn.word.id, 'word_name': defn.word.name, 'defn_id': defn.id }
         used_definitions = [d.id for d in selected]
      
      quiz_uuid = str(uuid.uuid4())
      survey_json = {
         "pages": [{"name": f"page{i}", "elements": [question]} for i, question in enumerate(questions)],
         "progressBarLocation": "top",
         "showProgressBar": "top",
         "showQuestionNumbers": "on",
         "progressBarType": "questions",
         "completedHtml": render_to_string('partial/_quiz_completed.html', {'quiz_uuid': quiz_uuid, 'lang_id': lang_id}, request)
      }
      if use_timer:
         time_settings = {
            "timeLimit": len(questions) * time_per_question,
            "timeLimitPerPage": time_per_question,
            "showTimerPanel": "bottom",
            "showTimerPanelMode": "page",
         }

         survey_json.update(time_settings)

      
      quizzes = request.session.get('quizzes', {})
      quizzes[quiz_uuid] = {
         'creation_date': django_tz.now().isoformat(),
         'quiz_type': quiz_type,
         'survey_json': survey_json,
         'answer_key': answer_key,
         'used_settings': used_settings,
         'used_definitions_id_list': used_definitions,
         'definition_distractor_map': definition_and_distractors
      }
      request.session['quizzes'] = quizzes
      
      kwargs = {'lang_id': lang_id, 'quiz_uuid': quiz_uuid }
      if request.META.get('HTTP_HX_REQUEST'):
         return HttpResponse(headers={'HX-Redirect': reverse('quiz_play', kwargs=kwargs)})
      return redirect('quiz_play', **kwargs)

   return render(request, 'forms/_quiz_settings_form.html', {
      'lang_id': lang_id,
      'form': form
   })


@never_cache
def quiz_play(request, quiz_uuid, lang_id):
   language = get_object_or_404(Language, pk=lang_id)

   # print('received uuid', quiz_uuid)
   quiz_data = request.session['quizzes'].get(str(quiz_uuid))
   # print(request.session['quizzes'])
   if not quiz_data:
      messages.error(request, "The quiz has expired.")
      return redirect('decks', lang_id=lang_id)

   survey_json = quiz_data.get('survey_json')
   validation_url = reverse('validate_quiz_answer', kwargs={'lang_id': lang_id, 'quiz_uuid': quiz_uuid})
   remove_quiz_url = reverse('remove_quiz', kwargs={'lang_id': lang_id, 'quiz_uuid': quiz_uuid})

   return render(request, 'quiz_play.html', {
      'lang_id': lang_id,
      'survey_json': survey_json,
      'validation_url': validation_url,
      'remove_quiz_url': remove_quiz_url,
      'redirect_on_completion': False
   })


def clear_all_quizzes(request, lang_id):
   language = get_object_or_404(Language, pk=lang_id)
   
   quizzes = request.session.get('quizzes')
   if quizzes:
      request.session['quizzes'] = {}
   
   return redirect('decks', lang_id=lang_id)


def remove_quiz(request, quiz_uuid, lang_id):
   language = get_object_or_404(Language, pk=lang_id)

   quizzes = request.session.get('quizzes', {})
   quizzes.pop(str(quiz_uuid), None)
   
   request.session.modified = True
   
   return redirect('decks', lang_id=lang_id)


@require_POST
def validate_quiz_answer_ajax(request, quiz_uuid, lang_id):
   language = get_object_or_404(Language, pk=lang_id)

   data = json.loads(request.body)
   question_name = data.get('question_name')
   answer = data.get('answer')

   quiz_data = request.session.get('quizzes', {}).get(str(quiz_uuid), None)
   
   if not quiz_data:
      messages.error(request, "The quiz has expired.")
      return redirect('decks', lang_id=lang_id)
   
   answer_key = quiz_data['answer_key']
   
   correct_word_id, correct_word_name, defn_id = answer_key.get(question_name).values()
   
   defn = Definition.objects.filter(pk=defn_id).first()
   img = defn.image if defn else None 

   is_correct = (answer == correct_word_name)
   QuizAttempt.objects.create(correct=is_correct, deck=None, definition=defn)
   defn.update_forgetting_frequency()
   defn.reviewed_at = datetime.now(UTC)
   defn.save(update_fields=['reviewed_at'])

   correct_answer = {
      'correct': is_correct,
      'word_path': reverse('word_details', kwargs={'lang_id': lang_id, 'word_id': correct_word_id}),
      'word_name': correct_word_name,
      'definition': defn.description if defn else '',
      'image_path': defn.image.file.url if img and img.file else '',
      'image_id': img.id if img else '',
      'examples': [e.description for e in defn.examples.all()] if defn else []
   }
   
   return JsonResponse(correct_answer)


def save_quiz_to_deck(request, quiz_uuid, lang_id):
   language = get_object_or_404(Language, pk=lang_id)
   quizzes = request.session.get('quizzes', {})
   quiz_data = quizzes.get(str(quiz_uuid))
   
   if not quiz_data:
      messages.error(request, "The quiz has expired.")
      return redirect('decks', lang_id=lang_id)
   
   form = DeckForm(request.POST or None, request.FILES or None, lang_id=lang_id)
   
   if request.method == 'POST':
      used_definitions = quiz_data.get('used_definitions_id_list')
      defn_distract_map = quiz_data.get('definition_distractor_map')
      
      deck = form.save()
      if used_definitions:
         for defn_id in used_definitions:
            definition = Definition.objects.filter(pk=defn_id).first()
            if definition:
               question = DeckQuestion.objects.create(
                  deck=deck,
                  definition=definition
               )
      elif defn_distract_map:
         for correct_defn_id, distractor_ids in defn_distract_map.items():
            definition = Definition.objects.filter(pk=correct_defn_id).first()
            if definition:
               question = DeckQuestion.objects.create(deck=deck, definition=definition)
               distractor_objs = Word.objects.filter(pk__in=distractor_ids)
               question.distractors.add(*distractor_objs)

      quizzes.pop(str(quiz_uuid), None)
      request.session.modified = True
      
      if request.META.get('HTTP_HX_REQUEST'):
         return HttpResponse(headers={'HX-Redirect': reverse('decks', kwargs={'lang_id': lang_id})})
      return redirect('decks', lang_id=lang_id)
   
   return render(request, 'forms/_deck_form.html', {
      'form': form,
      'quiz_uuid': quiz_uuid,
      'lang_id': lang_id
   })

@require_POST
def deck_delete(request, lang_id, deck_id):
   deck = get_object_or_404(Deck, pk=deck_id)

   if request.method == 'POST':
      deck.delete()
      messages.success(request, "Deck deleted.")

      return redirect('decks', lang_id=lang_id)


def deck_quiz_settings(request, lang_id, deck_id):
   language = get_object_or_404(Language, pk=lang_id)
   language = get_object_or_404(Deck, pk=deck_id)
   form = DeckQuizSettingsForm(request.POST or None)

   if request.method == 'POST' and form.is_valid():
      quiz_type = form.cleaned_data.get('quiz_type')
      time_per_question = form.cleaned_data.get('time_per_question') or 0

      base_url = reverse("deck_quiz_play", kwargs={'lang_id': lang_id, 'deck_id': deck_id})
      redirect_url = f'{base_url}?quiz_type={quiz_type}&time_per_question={time_per_question}'

      if request.META.get('HTTP_HX_REQUEST'):
         return HttpResponse(headers={'HX-Redirect': redirect_url})
      return redirect(redirect_url)

   return render(request, 'forms/_deck_quiz_settings_form.html', {
      'lang_id': lang_id,
      'deck_id': deck_id,
      'form': form
   })


@never_cache
def deck_quiz_play(request, lang_id, deck_id):
   quiz_type = request.GET.get('quiz_type', 'WW')
   time_per_question = int(request.GET.get('time_per_question', 0))

   deck = get_object_or_404(
      Deck.objects.prefetch_related('deck_questions__definition', 'deck_questions__distractors'),
      pk=deck_id
   )

   is_mo = quiz_type == 'MO'

   pages = [
      {
         'name': f"page{idx}",
         'elements': [
            {
               'type': 'radiogroup' if is_mo else 'text',
               'name': f'q_{question.id}',
               'title': question.definition.description,
               **(
                  {
                     'choices': sorted(
                        [question.definition.word.name] + [w.name for w in question.distractors.all()],
                        key=lambda _: random.random()
                     )
                  }
                  if is_mo
                  else {}
               )
            }
         ]
      }
      for idx, question in enumerate(deck.deck_questions.all())
   ]
   
   survey_json = {
      "pages": pages,
      "progressBarLocation": "top",
      "showProgressBar": "top",
      "showQuestionNumbers": "on",
      "progressBarType": "questions",
      "showCompletedPage": False,
   }

   if time_per_question and time_per_question > 0:
      survey_json.update({
         "timeLimit": len(pages) * time_per_question,
         "timeLimitPerPage": time_per_question,
         "showTimerPanel": "bottom",
         "showTimerPanelMode": "page",
      })
      

   return render(request, 'quiz_play.html', {
      'lang_id': lang_id,
      'survey_json': survey_json,
      'validation_url': reverse('validate_deck_quiz_answer', kwargs={'lang_id': lang_id, 'deck_id': deck_id}),
      'redirect_on_completion': True
   })


@require_POST
def validate_deck_quiz_answer_ajax(request, lang_id, deck_id):
   language = get_object_or_404(Language, pk=lang_id)
   deck = get_object_or_404(Deck, pk=deck_id)

   data = json.loads(request.body)
   question_name = data.get('question_name')
   answer = data.get('answer')

   if not question_name:
      return JsonResponse({'error': 'Missing question name'}, status=400)

   question_id = question_name[2:]
   question = (
      DeckQuestion.objects
         .select_related('definition', 'definition__word', 'definition__image')
         .prefetch_related('definition__examples')
         .filter(pk=question_id, deck=deck)
         .first()
   )

   if not question:
      return JsonResponse({'error': 'Question not found in this deck'}, status=404)

   correct_definition = question.definition
   correct_word = correct_definition.word
   image = correct_definition.image if correct_definition else None

   is_correct = (correct_word.name == answer)

   QuizAttempt.objects.create(correct=is_correct, deck=deck, definition=correct_definition)
   correct_definition.update_forgetting_frequency()
   correct_definition.reviewed_at = datetime.now(UTC)
   correct_definition.save(update_fields=['reviewed_at'])
   
   correct_answer = {
      'correct': is_correct,
      'word_path': reverse('word_details', kwargs={'lang_id': lang_id, 'word_id': correct_word.id}),
      'word_name': correct_word.name,
      'definition': '',
      'image_path': image.file.url if image and image.file else '',
      'image_id': image.id if image and image.file else '',
      'examples': [e.description for e in correct_definition.examples.all()] if correct_definition else []
   }
   
   return JsonResponse(correct_answer)


def deck_quiz_questions(request, lang_id, deck_id=None):
   deck = get_object_or_404(Deck, pk=deck_id) if deck_id else None
   language = get_object_or_404(Language, pk=lang_id)

   if request.method == 'POST':
      try:
         questions = json.loads(request.POST.get('questions', '[]'))
      except json.JSONDecodeError as e:
         print(f"Invalid JSON payload: {e}")
         return JsonResponse({'error': 'Invalid JSON'}, status=400)
      
      try: 
         with transaction.atomic():
            deck_form = DeckForm(request.POST, request.FILES, instance=deck, lang_id=lang_id)
            if not deck_form.is_valid():
               return JsonResponse({'errors': deck_form.errors}, status=400)
            
            deck = deck_form.save()
            processed_definition_ids = []
            for q in questions:
               q['deck'] = deck.id
               defn_id = q.get('definition')
               
               instance = DeckQuestion.objects.filter(
                  deck=deck,
                  definition_id=defn_id
               ).first()

               question_form = DeckQuestionForm(data=q, instance=instance)

               if question_form.is_valid():
                  question_form.save()
                  processed_definition_ids.append(defn_id)
               else:
                  return JsonResponse({'errors': question_form.errors}, status=400)
               
            DeckQuestion.objects.filter(deck=deck).exclude(
               definition_id__in=processed_definition_ids
            ).delete()

            delete_image = request.POST.get("delete_image", '')

            if delete_image and delete_image != 'false' and deck.image:
               img_to_del = deck.image
               storage = img_to_del.file.storage
               if storage.exists(img_to_del.file.name):
                  storage.delete(img_to_del.file.name)
               img_to_del.delete()
               deck.image = None

      except Exception as e:
         print(f"Unexpected error: {type(e).__name__} - {e}")
         return JsonResponse({'error': 'Server error'}, status=500)

      return JsonResponse({'success': True}, status=200)

   initial_data = {
      'name': deck.name if deck else '',
      'description': deck.description if deck else '',
      'questions': [
         {
            'word_text': q.definition.word.name,
            'word_id': q.definition.word.id,
            'definition_id': q.definition.id,
            'distractors': [{'id': word.id, 'text': word.name} for word in q.distractors.all()],
         }
         for q in DeckQuestion.objects.prefetch_related('distractors', 'definition__word').filter(deck=deck)
      ]
   } if deck else None
   
   image_url = deck.image.file.url if (deck and getattr(deck, 'image', None) and deck.image.file) else None
   deck_form = DeckForm(image_path=image_url, lang_id=lang_id)

   return render(request, 'forms/_deck_quiz_questions.html', {
      'lang_id': lang_id,
      'deck_id': deck_id,
      'deck_name': deck.name if deck else "",
      'deck_quiz_initial_data': initial_data,
      'deck_form': deck_form
   })


@require_POST
def mark_defn_as_reviewed(request, lang_id, defn_id):
   language = get_object_or_404(Language, pk=lang_id)
   definition = get_object_or_404(Definition, pk=defn_id)
   # tooltip_content = '<b>Reviewed at:</b><br>'

   definition.reviewed_at = datetime.now(UTC)
   definition.save(update_fields=['reviewed_at'])
   # formatted = date_format(django_tz.localtime(definition.reviewed_at), "DATETIME_FORMAT") if definition.reviewed_at else 'Never'
   # tooltip_content += f"Defn {idx+1}: {formatted}<br>"

   return JsonResponse({'success': True}, status=200)


def show_definitions_to_review(request, lang_id, word_id):
   word = get_object_or_404(
      Word.objects.prefetch_related('definitions'),
      pk=word_id
   )

   return render(request, 'partial/_mark_reviewed_modal.html', {'word': word})


def get_reviewed_at_dates(request, lang_id, word_id):
   language = get_object_or_404(Language, pk=lang_id)
   word = get_object_or_404(
      Word.objects.prefetch_related(
         Prefetch('definitions', queryset=Definition.objects.order_by('pk'))
      ),
      pk=word_id
   )
   dates = [
      date_format(django_tz.localtime(date), "DATETIME_FORMAT") if date else None
      for date in word.get_definitions_reviewed_at()
   ]
   return JsonResponse({'success': True, 'dates': dates}, status=200)