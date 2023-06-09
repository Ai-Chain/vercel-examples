import json
from typing import Type, Optional, Dict, Any, List

import langchain
from langchain.chains import ChatVectorDBChain
from langchain.chains.llm import LLMChain
from langchain.chains.question_answering import load_qa_chain
from langchain.docstore.document import Document
from pydantic import HttpUrl
from pytube import YouTube
from steamship import File, Task, Tag, SteamshipError
from steamship.invocable import Config
from steamship.invocable import PackageService, post, get
from steamship_langchain import OpenAI
from steamship_langchain.vectorstores import SteamshipVectorStore

from chat_history import ChatHistory
from prompts import qa_prompt, condense_question_prompt

langchain.llm_cache = None

DEBUG = False


class AskMyCourse(PackageService):
    class AskMyCourseConfig(Config):
        index_name: str
        default_chat_session_id: Optional[str] = "default"

    config: AskMyCourseConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.youtube_importer = self.client.use_plugin("youtube-file-importer")
        self.transcriber = self.client.use_plugin("s2t-blockifier-default")
        self.qa_chain = self._get_chain()

    @classmethod
    def config_cls(cls) -> Type[Config]:
        return cls.AskMyCourseConfig

    def _get_index(self):
        return SteamshipVectorStore(
            client=self.client,
            index_name=self.config.index_name,
            embedding="text-embedding-ada-002",
        )

    def _get_chain(self):
        doc_index = self._get_index()

        doc_chain = load_qa_chain(
            OpenAI(client=self.client, temperature=0, verbose=DEBUG),
            chain_type="stuff",
            prompt=qa_prompt,
            verbose=DEBUG,
        )
        question_chain = LLMChain(
            llm=OpenAI(client=self.client, temperature=0, verbose=DEBUG),
            prompt=condense_question_prompt,
        )
        return ChatVectorDBChain(
            vectorstore=doc_index,
            combine_docs_chain=doc_chain,
            question_generator=question_chain,
            return_source_documents=True,
            top_k_docs_for_context=2,
        )

    @get("/lectures", public=True)
    def get_lectures(self) -> List[Dict[str, Any]]:
        files = File.list(self.client).files
        lectures = []
        for file in files:
            source_tags = [tag for tag in file.tags if tag.kind == "source"]
            status_tags = [tag for tag in file.tags if tag.kind == "status"]
            title_tags = [tag for tag in file.tags if tag.kind == "title"]
            if source_tags and status_tags:
                lectures.append(
                    {
                        "source": source_tags[0].name if source_tags else "unknown",
                        "status": status_tags[0].name if status_tags else "unknown",
                        "title": title_tags[0].name if title_tags else "unknown",
                    }
                )
        return lectures

    def _update_file_status(self, file: File, status: str) -> None:
        file = file.refresh()
        status_tags = [tag for tag in file.tags if tag.kind == "status"]
        for status_tag in status_tags:
            try:
                status_tag.client = self.client
                status_tag.delete()
            except SteamshipError:
                pass

        Tag.create(self.client, file_id=file.id, kind="status", name=status)

    @post("/index_lecture")
    def index_lecture(self, task_id: str, source: str) -> bool:
        file_create_task = Task.get(self.client, task_id)
        file = File.get(self.client, json.loads(file_create_task.output)["file"]["id"])
        self._update_file_status(file, "Indexing")
        tags = file.blocks[0].tags

        timestamps = [tag for tag in tags if tag.kind == "timestamp"]
        timestamps = sorted(timestamps, key=lambda x: x.start_idx)

        context_window_size = 200
        context_window_overlap = 50

        documents = []
        for i in range(
                0, len(timestamps), context_window_size - context_window_overlap
        ):
            timestamp_tags_window = timestamps[i: i + context_window_size]
            page_content = " ".join(tag.name for tag in timestamp_tags_window)
            doc = Document(
                page_content=page_content,
                metadata={
                    "start_time": timestamp_tags_window[0].value["start_time"],
                    "end_time": timestamp_tags_window[-1].value["end_time"],
                    "start_idx": timestamp_tags_window[-1].start_idx,
                    "end_idx": timestamp_tags_window[-1].end_idx,
                    "source": source,
                },
            )
            documents.append(doc)
        self._get_index().add_documents(documents)
        self._update_file_status(file, "Indexed")
        return True

    @post("/transcribe_lecture")
    def transcribe_lecture(self, task_id: str, source: str):
        file_create_task = Task.get(self.client, task_id)
        file = File.get(self.client, json.loads(file_create_task.output)["file"]["id"])

        Tag.create(self.client, file_id=file.id, kind="source", name=source)
        Tag.create(self.client, file_id=file.id, kind="title", name=YouTube(source).title)

        self._update_file_status(file, "Transcribing")

        transcribe_lecture_task = file.blockify(self.transcriber.handle)

        return self.invoke_later(
            method="index_lecture",
            arguments={"task_id": file_create_task.task_id, "source": source},
            wait_on_tasks=[transcribe_lecture_task],
        )

    @post("/add_lecture")
    def add_lecture(self, youtube_url: HttpUrl) -> bool:
        file_create_task = File.create_with_plugin(
            self.client, plugin_instance=self.youtube_importer.handle, url=youtube_url
        )
        self.invoke_later(
            method="transcribe_lecture",
            arguments={"task_id": file_create_task.task_id, "source": youtube_url},
            wait_on_tasks=[file_create_task],
        )

        return True

    @post("/answer", public=True)
    def answer(
            self, question: str, chat_session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        chat_session_id = chat_session_id or self.config.default_chat_session_id
        chat_history = ChatHistory(self.client, chat_session_id)

        result = self.qa_chain(
            {"question": question, "chat_history": chat_history.load()}
        )
        if len(result["source_documents"]) == 0:
            return {
                "answer": "No sources found to answer your question. Please try another question.",
                "sources": result["source_documents"],
            }

        answer = result["answer"]
        sources = result["source_documents"]
        chat_history.append(question, answer)

        return {"answer": answer.strip(), "sources": sources}
