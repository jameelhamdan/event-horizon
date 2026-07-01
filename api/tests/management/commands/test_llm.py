import time

from django.core.management.base import BaseCommand

from services.llm import get_llm_service


class Command(BaseCommand):
    help = 'Send one example request through the LLM route for a role and print the result.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--role',
            default='default',
            help='LLM route role to test (e.g. analyzer_lite, topics, default).',
        )
        parser.add_argument(
            '--prompt',
            default=(
                'Classify this headline into one category '
                '(conflict/protest/disaster/political/economic/crime/general): '
                '"Central bank raises interest rates amid inflation". '
                'Answer with only the category word.'
            ),
            help='Prompt to send.',
        )

    def handle(self, *args, **options):
        svc = get_llm_service(options['role'])
        self.stdout.write(f'Role: {options["role"]}')
        self.stdout.write(f'Resolved: {type(svc).__name__}  Model/chain: {getattr(svc, "_model", "?")}')
        self.stdout.write(f'Prompt: {options["prompt"]}')
        start = time.time()
        out = svc.complete(options['prompt'])
        elapsed = time.time() - start
        self.stdout.write(self.style.SUCCESS(f'Elapsed: {elapsed:.2f}s'))
        self.stdout.write(f'Response: {out!r}')
