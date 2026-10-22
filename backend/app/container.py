"""Wires all services together (one instance per FastAPI app)."""
from __future__ import annotations

from dataclasses import dataclass

from app.core.settings import AppSettings
from app.providers.fake import FakeScript
from app.providers.registry import ProviderRegistry
from app.services.attachments import AttachmentService
from app.services.budget import BudgetGuard, ModelRouter
from app.services.chair import ChairOrchestrator
from app.services.compression import CompressionService
from app.services.debate import DebateController
from app.services.events import EventBus
from app.services.evidence import EvidenceService
from app.services.llm_runner import LLMRunner
from app.services.packets import PacketService
from app.services.retrieval import DocumentRetriever
from app.services.usage import UsageMeter
from app.storage.config_store import ConfigStore
from app.storage.db import Database
from app.storage.secret_store import SecretStore


@dataclass
class Container:
    settings: AppSettings
    db: Database
    secrets: SecretStore
    config: ConfigStore
    bus: EventBus
    registry: ProviderRegistry
    meter: UsageMeter
    guard: BudgetGuard
    router: ModelRouter
    runner: LLMRunner
    files: AttachmentService
    retriever: DocumentRetriever
    packets: PacketService
    chair: ChairOrchestrator
    evidence: EvidenceService
    compression: CompressionService
    debate: DebateController


def build_container(settings: AppSettings, fake_script: FakeScript | None = None) -> Container:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    secrets = SecretStore(settings.env_path)
    config = ConfigStore(settings.config_dir, settings.roles_dir, enable_fake=settings.enable_fake_provider)
    bus = EventBus(db)
    registry = ProviderRegistry(fake_script)
    meter = UsageMeter(db)
    guard = BudgetGuard(db, meter)
    router = ModelRouter(guard, secrets)
    runner = LLMRunner(db, bus, registry, router, guard, meter, secrets)
    files = AttachmentService(settings, db, bus)
    retriever = DocumentRetriever(db)
    packets = PacketService(db, bus)
    chair = ChairOrchestrator(db, bus, runner, retriever, packets, files)
    evidence = EvidenceService(db, bus, chair, packets)
    compression = CompressionService(db, bus, runner)
    debate = DebateController(db=db, bus=bus, config=config, runner=runner, chair=chair, packets=packets,
                              evidence=evidence, files=files, guard=guard, compression=compression,
                              registry=registry, secrets=secrets)
    return Container(settings, db, secrets, config, bus, registry, meter, guard, router, runner, files, retriever,
                     packets, chair, evidence, compression, debate)
