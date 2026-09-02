# SQL-of-Thought：基于引导式纠错的多智能体文本到SQL转换框架

## 作者
- Saumya Chaturvedi — 马克斯·普朗克软件系统研究所，德国萨尔布吕肯 (schaturv@mpi-sws.org)
- Aman Chadha — AWS GenAI，美国加利福尼亚州圣克拉拉 (hi@aman.ai)
- Laurent Bindschaedler — 马克斯·普朗克软件系统研究所，德国萨尔布吕肯 (bindsch@mpi-sws.org)

> arXiv:2509.00581v2 [cs.DB] 2025年9月28日
> 预印本，审稿中

---

## 摘要

将自然语言查询转换为SQL查询是工业界和学术界面临的一个关键挑战，旨在提升数据库和大规模应用的可访问性。本文研究了如何利用上下文学习和思维链（Chain-of-Thought）技术来开发稳健的文本到SQL（Text-to-SQL）解决方案。我们提出SQL-of-Thought：一个多智能体框架，将Text2SQL任务分解为模式链接（Schema Linking）、子问题识别、查询计划生成、SQL生成以及引导式纠错循环。与以往仅依赖基于执行的静态纠错系统不同，我们引入了基于上下文学习（In-Context Learning）的、以分类法为指导的动态错误修正机制。SQL-of-Thought在Spider数据集及其变体上取得了最先进的结果，将引导式错误分类法与基于推理的查询计划相结合。

---

## 1. 引言

文本到SQL（NL2SQL）已成为研究和实际应用中的一个重要问题，使非技术用户能够通过自然语言查询结构化数据库。早期的序列到序列（sequence-to-sequence）和模式感知模型虽然提高了可访问性，但在泛化方面往往表现不佳。最近，大语言模型（LLM）和提示技术的进展显著提升了性能，DIN-SQL [11] 和 DAIL-SQL [5] 等方法将任务分解为多个子任务。然而，正如 Li 等人 [7] 和 Biswal 等人 [1] 所指出的，即使是最先进的系统在处理真实查询时仍然脆弱。这是因为仅靠基于执行的反馈无法纠正语法正确但逻辑错误的SQL查询。

多智能体方法 [21, 12, 14] 和推理引导提示 [19, 10] 已显示出弥合这一差距的潜力。多智能体系统增强了模块化和专业化；然而，它们的纠错仍然严重依赖执行信号或静态重新生成。另一方面，基于推理的方法改进了查询计划，但无引导的推理往往会引入新的错误或低效之处。所需的是一个系统化的方法来将多智能体分解与结构化推理和可解释的纠错相结合。

我们通过SQL-of-Thought来解决这一问题。SQL-of-Thought是一个旨在增强SQL查询的多智能体框架。该框架包含以下关键组件：(i) 专门用于模式链接、子问题识别、查询计划生成、SQL合成和错误纠正的智能体；(ii) 一个以分类法为指导的纠错循环，对SQL失败模式进行分类并通过思维链推理过程进行修正；以及 (iii) 对不同智能体角色中推理模型和非推理模型的分析比较。我们的框架在Spider [22]、Spider-Realistic [3] 和 Spider-SYN [4] 上实现了最先进的执行准确率，同时通过混合模型展示了有效的成本-性能权衡。这些结果表明，紧凑的引导式推理配合结构化的错误反馈，比仅依赖基于执行的细化更可靠。

**本文贡献如下：**

- **SQL-of-Thought**：一个新颖的NL2SQL多智能体解决方案，包含模式链接、子问题识别、用于查询计划生成的思维链（CoT）、SQL生成以及显式的纠错循环。
- **引导式错误修正**：由全面的错误分类法和上下文学习驱动。
- **对推理和非推理模型选择的详细分析**：适用于SQL-of-Thought框架。
- **最先进的结果**：在Spider [22] 基准测试及其变体Spider-Realistic [3] 和 Spider-SYN [4] 上达到最优性能。

---

## 2. 相关工作

### 2.1 NL2SQL的历史

早期的Text-to-SQL工作使用主流的序列到序列架构，如编程语言模型（PLM），例如RESDSQL [8]，它从问题中识别相关模式，然后利用模式构造SQL查询。其他选择包括图神经网络（GNN）、循环神经网络（RNN）、Transformer，以及最近的大语言模型（LLM）用于自然语言到SQL的转换。随着LLM的出现，在GPT [2, 9] 和Claude等强大模型上进行提示工程，为NL2SQL提供了细粒度和更快速的解决方案。Shi等人 [16] 强调了基于LLM的解决方案在所有其他编程文本到SQL替代方案中的优越性。

DIN-SQL [11] 尝试了带有纠错的上下文学习，分解子问题，并定义不同的提示模板来处理每个子任务。然而，他们的方法仅在失败时重新生成提示，没有具体的错误信息。DAIL-SQL [5] 设计了复杂的SQL代码风格提示模板来编码问题和数据库模式。这些模板为模式链接和SQL查询生成提供少样本示例，利用GPT-4生成解决方案。DAILSQL-SC采用自一致性（Self-Consistency）对解决方案进行后处理以进一步精炼。

一些研究 [7] 讨论了NL2SQL的生产就绪性和评估框架，使用各种提示方法，再次验证了基于LLM的方法通过更高的可靠性和泛化性普遍优于预训练的任务特定模型。他们将查询分为四类：子查询、连接（JOIN）、逻辑连接符和ORDER BY子句，以衡量模型在不同难度级别上的效能。TAG框架 [1] 展示了现有模型在复杂真实查询上的失败，指出大多数NL2SQL模型和基准测试仅对约20%的真实用户查询有效。他们引入了一个推理框架，将问题分解为三个阶段：(i) 通过NL2SQL解析进行查询合成，(ii) 在底层数据库上进行查询执行，以及 (iii) 答案生成，LLM整合自然语言查询和执行结果以产生最终的人类可读答案。

### 2.2 智能体方法

Tool-SQL [21] 开创了工具优先的智能体框架，LLM驱动的智能体借助两个工具迭代生成和优化SQL查询：数据库检索器用于解决条件不匹配问题，错误检测器用于更严格的约束验证。该方法主要解决数据库不匹配问题，如缺失高亮、改写释义等类似问题。

Chase SQL [12] 定义了一个复杂的框架，包含值检索、多个候选生成器和查询修复器以及选择智能体。候选者通过"EXPLAIN"关键字提示LLM生成查询计划。Shao等人 [14] 提出的多智能体框架引入了一个四智能体系统，其中专门的角色（开发者、研究员、执行者、专家）在简单和复杂的NL2SQL查询上协作。通过结合示例检索、思维链推理、语法验证和错误驱动的细化，该框架提高了正确性和执行效率。

### 2.3 推理引导的NL2SQL解决方案

ACT-SQL [23] 实验了提示风格和少样本示例选择策略，自动化为自然语言到SQL解决方案选择提示，使过程经济且省时。Think2SQL [10] 及相关方法通过结合零样本提示、监督微调和强化学习来探索文本到SQL的推理。他们研究了推理是否能改进文本到SQL、如何最好地训练具有推理能力的模型，以及基于执行的奖励是否足够。他们的结果显示了混合证据：推理模型通常在查询计划方面表现更好，但在不同数据集上并不一致。他们还通过手动提供子集简化了模式链接，限制了评估范围。Tai等人 [19] 强调了思维链提示技术，在Spider数据集上取得了显著收益，并提到过于详细的推理可能会传播错误。

我们的工作不同之处在于，我们不仅在计划阶段使用思维链推理方法，还在模式链接、子问题分解和纠错中使用，并由明确的分类法进行引导。我们还在各种模型上评估方法，包括推理模型和通用LLM，以验证模型CoT能力的重要性。

---

## 3. 方法论

提出的SQL-of-Thought框架实现为一个LLM驱动的文本到SQL管道，生成可执行的SQL查询Y。该过程可以形式化为：

**Y = LLM(Q, S, C, P, T | θ)**

其中Q是自然语言问题，S是链接的模式，C表示子句级别的子问题，P是查询计划，T表示用于CoT引导纠错的错误分类法。LLM(.|θ)是由参数θ参数化的语言模型。

### 3.1 SQL-of-Thought

SQL-of-Thought框架的详细构成如下：

**模式链接智能体（Schema Linking Agent）**：模式链接智能体解析自然语言问题，结合数据库模式（通过db_id标识），识别回答问题所需的相关表和列。此外，它提取结构信息，如主键、外键和连接关系。这种表示为后续步骤奠定了基础，将SQL生成限制在与模式相关的实体范围内。

**子问题智能体（Subproblem Agent）**：给定自然语言问题和模式链接输出，子问题智能体将查询分解为子句级别的子问题（例如，WHERE、GROUP BY、JOIN、DISTINCT、ORDER BY、HAVING、EXCEPT、LIMIT、UNION）。每个识别出的子句在结构化JSON对象中表示为一对键值，键是子句类型，值是部分完成的子句表达式。这种分解提供了查询意图的模块化表示，使下游智能体能够对更小、更明确的单元进行推理。

**查询计划智能体（Query Plan Agent）**：查询计划智能体生成一个逐步执行计划，将用户意图映射到模式和子问题。与以往工作（如Chase-SQL [12]）将计划视为表面级映射不同，我们的智能体被明确提示执行思维链推理，解释中间决策。这种结构化推理鼓励问题、子问题和最终SQL查询之间更深层的对齐。查询计划智能体仅产生程序化计划，并在此阶段明确限制生成可执行的SQL。

**SQL智能体（SQL Agent）**：SQL智能体接收自然语言问题和查询计划，生成可执行的SQL查询。后处理移除多余的项目，如尾部分号或自然语言片段，确保查询语法有效。生成的查询随后在数据库上执行，结果与真实答案比较。如果查询失败，管道将转换到纠错循环。

**纠错计划智能体（Correction Plan Agent）**：纠错计划智能体通过分析自然语言问题、模式和执行结果上下文中的失败SQL查询来启动纠错循环。与DIN-SQL或DAIL-SQL [5, 11] 等仅依赖执行反馈的系统不同，该智能体额外受到来自Shen等人 [15] 的错误分类法的引导。该分类法对常见错误模式进行分类（例如，模式不匹配、连接不一致、聚合误用），智能体生成思维链计划，描述如何解决已识别的错误。受反思性学习方法 [17] 的启发，智能体迭代生成结构化反馈，为纠错SQL生成过程提供信息。

**纠错SQL智能体（Correction SQL Agent）**：纠错SQL智能体接收纠错计划、问题、模式和错误的SQL查询作为输入。基于结构化指导，它重新生成SQL查询，同时避免先前的错误。修正后的查询在数据库上重新执行，如果结果仍然不匹配，则重新进入纠错循环，直到收敛或达到最大纠错尝试次数。

### 3.2 错误分类法

我们纠错循环中采用的错误分类法源自Shen等人 [15] 提出的分类，系统地对NL2SQL系统中遇到的标准失败模式进行分类。我们提出的分类法在此基础上扩展，创建了可由LLM识别和纠正的系统化错误类别，以及每个类别的全面子类型。

我们提供简明的错误代码而非冗长的解释，以便于识别并防止LLM上下文窗口溢出。我们的分类法涵盖广泛的问题，包括语法错误（例如，无效别名或格式错误的SQL）、模式链接错误（例如，缺失或不明确的列、错误的外键）以及连接相关的错误（如缺失连接、错误的连接类型或包含多余的表）。它还进一步涵盖过滤条件错误（例如，WHERE子句中错误的列、类型不匹配）、聚合逻辑错误（例如，缺少GROUP BY、误用HAVING），以及值表示错误，如硬编码值或格式不匹配。更高级的类别捕获子查询制定中的失败（例如，未使用或错误关联的子查询）、集合操作（UNION、INTERSECT、EXCEPT），以及其他结构性疏忽，如缺少ORDER BY或LIMIT子句。

通过明确编码这些错误类型，分类法提供了细粒度的诊断视角，使纠错计划智能体不仅能推理出什么失败了，还能推理出在SQL生成管道中为什么失败，范围从基本语法问题到复杂的子查询和连接制定错误，为纠正提供具体指导。

将这种分类法纳入纠错循环使系统能够超越粗粒度的基于执行的反馈（如DIN-SQL [11] 和DAIL-SQL [5] 所用），而是为迭代细化提供可解释的、基于语言的指导。我们向纠错计划智能体提供总结的错误分类法以及思维链推理模板，使智能体能够识别失败的根本原因，将错误与模式或语法约束对齐，并提出具体的修复策略。这种方法与反思性学习方法 [17] 的发现一致，即LLM从口头化的自我反馈中受益，以增强编程任务的决策能力。因此，通过利用Shen等人 [15] 的结构化分类法，多智能体系统建立了错误检测、诊断和引导式纠正的原则性循环，从而弥合了原始执行反馈与系统化、可解释的查询细化之间的差距。

**错误分类法包含9个主要类别和31个子类别：**

| 类别 | 子类别 |
|------|--------|
| **语法（Syntax）** | sql_syntax_error, invalid_alias |
| **模式链接（Schema Link）** | table_missing, col_missing, ambiguous_col, incorrect_foreign_key |
| **连接（Join）** | join_missing, join_wrong_type, extra_table, incorrect_col |
| **过滤（Filter）** | where_missing, condition_wrong_col, condition_type_mismatch |
| **聚合（Aggregation）** | agg_no_groupby, groupby_missing_col, having_without_groupby, having_incorrect, having_vs_where |
| **值（Value）** | hardcoded_value, value_format_wrong |
| **子查询（Subquery）** | unused_subquery, subquery_missing, subquery_correlation_error |
| **集合操作（Set Operations）** | union_missing, intersect_missing, except_missing |
| **其他问题（Other Issues）** | order_by_missing, limit_missing, duplicate_select, unsupported_function, extra_values_selected |

---

## 4. 实验设置

### 4.1 数据集

我们使用Spider数据集 [22] 评估多智能体框架。Spider提供了多样化的数据库模式和任务，包括20个数据库设置和开发集中1034个文本-SQL对。我们避免使用BIRD-SQL基准测试，因为Shen等人 [15] 报告了其中大量的标注错误。BIRD-SQL的集合操作问题比Spider少，并且还为查询提供了证据字段（evidence field），这可能在查询生成时增加幻觉。为此，我们在Spider及其流行变体上测试：

- **Spider Realistic [3]**：基于Spider开发集的评估集，移除了列名的显式提及，包含508个样本。
- **Spider SYN [4]**：Spider评估数据集的一个具有挑战性的变体，通过对自然语言问题进行同义词替换手动修改构建。

### 4.2 评估指标

尽管我们在实验中观察了精确匹配（Exact Match）和有效SQL生成等指标，但我们仅使用**执行准确率（Execution Accuracy, EA）**进行整体评估。一个自然语言问题可能有多个SQL查询来回答它。LLM经常过度简化，在Text-to-SQL任务中，它们被发现将变量分配给子查询的结果，这就是为什么精确匹配（EM）不能准确表示过程准确性的原因。因此，我们采用执行准确率（EA）作为评估过程的指标。EA是一个布尔字段，比较生成查询的执行结果与数据集中存在的金标准SQL标签的执行结果。由于EA计算需要生成的SQL查询在Spider的SQLite文件上执行，我们还采用SQLite [18] 进行查询生成。

### 4.3 硬件与配置

所有实验在配备两块NVIDIA H100 GPU的机器上进行，每块80GB显存。我们使用闭源LLM（GPT-4o、GPT-5、Claude Opus 3）通过API进行评估。

---

## 5. 结果

### 5.1 Spider数据集上的执行准确率

SQL-of-Thought在Spider开发集上取得了最先进的结果。主要结果如下：

| 方法 | 执行准确率 (EA) |
|------|:---:|
| **SQL-of-Thought (Claude Opus 3)** | **95%** |
| SQL-of-Thought (GPT-5) | 89% |
| SQL-of-Thought (GPT-4o Mini) | 87% |
| SQL-of-Thought (GPT-3.5) | 67% |
| DIN-SQL + GPT-4 | 85.3% |
| DAIL-SQL + GPT-4 | 86.6% |
| Chase-SQL + GPT-4 | 87.3% |

### 5.2 Spider变体上的表现

在Spider-Realistic数据集上，SQL-of-Thought达到**91%**的执行准确率。在Spider-SYN数据集上达到**89%**。

### 5.3 消融实验

我们对SQL-of-Thought进行了消融研究，移除关键组件以衡量其贡献：

| 模型 | 完整框架 | 无纠错循环 | 无查询计划生成 |
|------|:---:|:---:|:---:|
| Claude 3 Opus | 95 | 85 | 90 |
| GPT-5 | 89 | 85 | 88 |
| GPT-4o Mini | 87 | 72 | 79 |
| GPT-3.5 | 67 | 59 | 73 |

我们还报告，使用Claude Opus 3在Spider数据集上运行一次SQL-of-Thought耗时5小时，token成本为$42.58（价格为$15/百万token）。

---

## 6. 讨论

### 6.1 推理模型 vs. 非推理模型

推理模型在几乎所有专门智能体任务中表现出明显优势，包括模式链接、查询规划、思维链和子句识别。非推理模型往往无法正确分解查询，产生有效但逻辑错误的SQL。常见的错误包括：将表A中的列A混淆并列为表B下的列B、忘记包含聚合GROUP BY子句、选择多余的或缺少的列等。然而，仅靠推理并不能在所有情况下保证改进。例如，没有错误分类法的无引导推理导致更差的纠错，常常导致在不同尝试中重复相同的纠正步骤。这凸显了动态引导纠错循环如何补充思维链风格的提示，帮助模型从直接示例和错误手册中学习。

### 6.2 经验总结

- 使用Claude Opus 3而非GPT提高了准确率。
- 引导式纠错循环（智能体依赖错误分类法）减少了重复并改善了逻辑纠正。
- 在SQL合成之前进行基于推理的查询计划生成也持续改善了结果，因为LLM已被证明在数学、编程和计划任务中更擅长制定推理。
- 在使用Claude 3 Opus + SQL-of-Thought的消融研究中，绕过查询计划智能体直接让SQL智能体生成查询，在100样本测试集上使错误查询率增加了5%。这些结果突显了分阶段推理的重要性：查询计划合成后再进行SQL生成是提高NL2SQL系统正确性的关键设计选择。

### 6.3 失败消融的经验教训

并非所有设计选择都能提高准确率；几个变体表现不佳，突显了引导式推理的重要性：

- 一个以自由形式应用完整错误分类法并将错误直接发送给SQL智能体的批评循环表现不佳。当错误首先通过查询计划智能体路由时，准确率得到改善，表明LLM在无引导调试中挣扎，并受益于至少一个结构化的推理步骤。
- 将GPT-4o的温度提高到0以上增加了表面多样性，但降低了计划的忠实度，产生了更多无效连接和子句误用。
- 在智能体提示中为特定子句（例如，JOIN、LIMIT）添加特定规则虽然膨胀了上下文窗口，并常常用无关细节分散模型注意力，降低了准确率。
- 一个具有多个修复智能体的消融设计，每个智能体处理特定类型的子问题错误并馈入聚合智能体生成最终SQL解决方案，失败了。独立编辑经常冲突，合并过程产生不连贯的SQL。
- 通过共享草稿板在纠错尝试之间携带历史记录扩大了上下文窗口，增加了延迟和API成本，并加剧了重复和模式漂移，导致准确率降低。

### 6.4 Token成本与其他指标

LLM在NL2SQL方面更好更快 [16, 7]，但API成本非常昂贵。特别是在多智能体框架或任何多管道系统中，每个样本的成本很高。提高准确率是以计算和昂贵过程为代价的。在Spider基准测试上运行一次，Claude Opus 3约花费$42.58，GPT模型约$44.2。

切换到更便宜的模型会导致准确率下降，但性能好的模型更昂贵。我们试图找到一个既有成本效益又高性能的中间地带。通过在不同智能体上使用Claude和GPT模型的各种组合进行推理，我们发现模式链接、查询计划生成和纠错计划生成等任务需要更高的推理能力，使用非推理模型时失败。这意味着子问题、SQL和纠错SQL智能体可以使用非推理模型，仍然表现相当。

这种方法实现了一个混合模型：对推理密集型智能体使用Claude Opus，对其他智能体使用GPT-4o，在约$30的成本下实现整个数据集运行的85%执行准确率（100样本消融实验）。

此外，我们还在开源模型上测试了SQL-of-Thought，如LLama-3.1-8B-Instruct [6] 和 Qwen2.5-1.5B [13] 等类似变体。我们发现这些模型不仅表现出高延迟（评估耗时长达三倍），而且性能显著不佳，在100样本集上仅达到约45.3%的准确率。这些模型在生成SQL方面有困难（导致长时间重复幻觉），跨不同表缺少列名，总体上不适合在NL2SQL框架中使用。未来的工作可以通过在单个智能体任务上微调小型语言模型（SLM）来提高此类框架的成本和效能。例如，可以设计错误数据库来微调纠错循环智能体。Spider [22] 为每个查询提供详细字段，包括是否使用LIMIT、INTERSECT、GROUPBY等子句。这些子句可用于创建微调子问题智能体的数据集。

---

## 7. 局限性

我们的评估仅限于Spider [22] 基准测试及其变体，可能无法完全捕捉真实数据库的挑战。错误分类法虽然有效，但尚未在多样化的查询结构上进行穷尽验证。多智能体设计也增加了推理成本，严重依赖闭源推理LLM。

未来的工作可以通过以下方式改进成本和效能：
- 为单个智能体任务微调小型语言模型（SLM），例如构建错误数据库来微调纠错循环智能体。
- 利用Spider的子句级别标注（例如LIMIT、INTERSECT、GROUPBY）来微调子问题智能体。

---

## 8. 结论

我们介绍了SQL-of-Thought，一个多智能体NL2SQL框架，结合了模式链接、子问题识别、思维链查询计划、SQL生成和以分类法为指导的纠错。该框架在Spider基准测试上实现了最先进的执行准确率，表明结构化推理和引导式纠错比仅依赖执行的反馈更有效。未来的工作应将评估扩展到真实世界数据集，并探索用于经济高效部署的微调SLM。

---

## 参考文献

[1] Asim Biswal, et al. Text2sql is not enough: Unifying ai and databases with tag, 2024. https://arxiv.org/abs/2408.14717

[2] Tom B. Brown, et al. Language models are few-shot learners, 2020. https://arxiv.org/abs/2005.14165

[3] Xiang Deng, et al. Structure-grounded pretraining for text-to-sql. NAACL 2021. doi: 10.18653/v1/2021.naacl-main.105

[4] Yujian Gan, et al. Towards robustness of text-to-sql models against synonym substitution, 2021. https://arxiv.org/abs/2106.01065

[5] Dawei Gao, et al. Text-to-sql empowered by large language models: A benchmark evaluation, 2023. https://arxiv.org/abs/2308.15363

[6] Aaron Grattafiori, et al. The llama 3 herd of models, 2024. https://arxiv.org/abs/2407.21783

[7] Boyan Li, et al. The dawn of natural language to sql: Are we fully ready? PVLDB, 17(11):3318-3331, 2024. doi: 10.14778/3681954.3682003

[8] Haoyang Li, et al. Resdsql: Decoupling schema linking and skeleton parsing for text-to-sql, 2023. https://arxiv.org/abs/2302.05965

[9] OpenAI, et al. Gpt-4 technical report, 2024. https://arxiv.org/abs/2303.08774

[10] Simone Papicchio, et al. Think2sql: Reinforce llm reasoning capabilities for text2sql, 2025. https://arxiv.org/abs/2504.15077

[11] Mohammadreza Pourreza and Davood Rafiei. Din-sql: Decomposed in-context learning of text-to-sql with self-correction, 2023. https://arxiv.org/abs/2304.11015

[12] Mohammadreza Pourreza, et al. Chase-sql: Multi-path reasoning and preference optimized candidate selection in text-to-sql, 2024. https://arxiv.org/abs/2410.01943

[13] Qwen, et al. Qwen2.5 technical report, 2025. https://arxiv.org/abs/2412.15115

[14] Zhihui Shao, et al. Enhancing text-to-SQL with question classification and multi-agent collaboration. NAACL 2025 Findings, pp. 4340-4349. doi: 10.18653/v1/2025.findings-naacl.245

[15] Jiawei Shen, et al. A study of in-context-learning-based text-to-sql errors, 2025. https://arxiv.org/abs/2501.09310

[16] Liang Shi, et al. A survey on employing large language models for text-to-sql tasks. ACM Comput. Surv., 2025. doi: 10.1145/3737873

[17] Noah Shinn, et al. Reflexion: Language agents with verbal reinforcement learning, 2023. https://arxiv.org/abs/2303.11366

[18] SQLite. https://github.com/sqlite/sqlite

[19] Chang-You Tai, et al. Exploring chain-of-thought style prompting for text-to-sql, 2023. https://arxiv.org/abs/2305.14215

[20] Bing Wang, et al. MAC-SQL: A multi-agent collaborative framework for text-to-SQL. COLING 2025, pp. 540-557.

[21] Zhongyuan Wang, et al. Tool-assisted agent on sql inspection and refinement in real-world scenarios, 2024. https://arxiv.org/abs/2408.16991

[22] Tao Yu, et al. Spider: A large-scale human-labeled dataset for complex and cross-domain semantic parsing and text-to-sql task, 2019. https://arxiv.org/abs/1809.08887

[23] Hanchong Zhang, et al. Act-sql: In-context learning for text-to-sql with automatically-generated chain-of-thought, 2023. https://arxiv.org/abs/2310.17342

---

*翻译完成日期：2025年7月16日*
*原始论文：SQL-of-Thought: Multi-agentic Text-to-SQL with Guided Error Correction — arXiv:2509.00581v2*
