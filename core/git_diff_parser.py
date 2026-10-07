"""
git_diff_parser.py
Turns a git commit / PR into structured data the Manager Agent can reason about.

Instead of handing raw diff text to an LLM (expensive, error-prone, hard to
control), we parse it into clean Python objects first. The LLM only ever sees
what it actually needs for a given task.
"""
from dataclasses import dataclass, field
from pathlib import Path

import git
from unidiff import PatchSet


# File extensions we actually want reviewed. Binary/lockfiles/etc are skipped.
REVIEWABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb",
    ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".sql",
}


@dataclass
class AddedLine:
    line_number: int
    content: str


@dataclass
class ChangedFile:
    path: str
    added_lines: list[AddedLine] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    is_new_file: bool = False
    is_deleted: bool = False
    full_diff_text: str = ""  # the raw hunk text, for showing agents full context

    @property
    def extension(self) -> str:
        return Path(self.path).suffix

    @property
    def is_reviewable(self) -> bool:
        return self.extension in REVIEWABLE_EXTENSIONS and not self.is_deleted


@dataclass
class ParsedDiff:
    base_commit: str
    head_commit: str
    changed_files: list[ChangedFile] = field(default_factory=list)

    @property
    def reviewable_files(self) -> list[ChangedFile]:
        
        return [f for f in self.changed_files if f.is_reviewable]


def get_file_content_at_commit(repo_path: str, commit_ref: str, file_path: str) -> str:
    """
    Fetch a file's full content as it exists at a given commit.
    Needed because tools like bandit need a real file's full content,
    not just the added-lines diff fragment.
    """
    repo = git.Repo(repo_path)
    commit = repo.commit(commit_ref)
    blob = commit.tree / file_path
    return blob.data_stream.read().decode("utf-8")


def parse_commit_range(repo_path: str, base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> ParsedDiff:
    """
    Parse the diff between two refs (commits, branches, or PR base/head) in a
    git repository into structured ChangedFile objects.
    """
    repo = git.Repo(repo_path)

    base_commit = repo.commit(base_ref)
    head_commit = repo.commit(head_ref)

    diff_text = repo.git.diff(base_commit.hexsha, head_commit.hexsha)

    patch_set = PatchSet(diff_text)

    changed_files = []
    for patched_file in patch_set:
        changed_file = ChangedFile(
            path=patched_file.path,
            is_new_file=patched_file.is_added_file,
            is_deleted=patched_file.is_removed_file,
            full_diff_text=str(patched_file),
        )

        for hunk in patched_file:
            for line in hunk:
                if line.is_added:
                    changed_file.added_lines.append(
                        AddedLine(line_number=line.target_line_no, content=line.value.rstrip("\n"))
                    )
                elif line.is_removed:
                    changed_file.removed_lines.append(line.value.rstrip("\n"))

        changed_files.append(changed_file)

    return ParsedDiff(
        base_commit=base_commit.hexsha[:7],
        head_commit=head_commit.hexsha[:7],
        changed_files=changed_files,
    )