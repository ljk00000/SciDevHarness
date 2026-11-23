using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Markdig;
using Markdig.Extensions.AutoIdentifiers;
using Markdig.Extensions.EmphasisExtras;
using Markdig.Extensions.Tables;
using Markdig.Extensions.TaskLists;
using Markdig.Renderers.Html;
using Markdig.Syntax;
using Markdig.Syntax.Inlines;

namespace SciDev.Desktop;

internal static class MarkdownReader
{
    private static readonly MarkdownPipeline Pipeline = new MarkdownPipelineBuilder().DisableHtml()
        .UsePipeTables().UseTaskLists().UseEmphasisExtras(EmphasisExtraOptions.Strikethrough)
        .UseAutoIdentifiers(AutoIdentifierOptions.GitHub).UseCjkFriendlyEmphasis().Build();
    private static readonly FontFamily Monospace = new("Consolas, Cascadia Mono");

    internal static MarkdownDocument Parse(string markdown) => Markdown.Parse(markdown, Pipeline);
    internal static string? LinkUrl(LinkInline link) => link.GetDynamicUrl?.Invoke() ?? link.Url;

    internal static FlowDocument Render(MarkdownDocument source, Func<string, string?> resolveLink, Action<string> open,
        IReadOnlyDictionary<string, BitmapSource>? images = null) => new Renderer(resolveLink, open, images).Render(source);

    private sealed class Renderer(Func<string, string?> resolve, Action<string> open, IReadOnlyDictionary<string, BitmapSource>? images)
    {
        private readonly Dictionary<string, Paragraph> _headings = new(StringComparer.OrdinalIgnoreCase);

        internal FlowDocument Render(MarkdownDocument source)
        {
            var document = new FlowDocument { PagePadding = new(25, 21, 25, 34), FontSize = 15, LineHeight = 25 };
            document.SetResourceReference(FlowDocument.ForegroundProperty, "WorkbenchText");
            AppendBlocks(document.Blocks, source);
            return document;
        }

        private void AppendBlocks(BlockCollection output, ContainerBlock source)
        {
            foreach (var child in source)
            {
                switch (child)
                {
                    case HeadingBlock heading:
                        output.Add(Heading(heading));
                        break;
                    case ParagraphBlock { Inline.FirstChild: LinkInline { IsImage: true, NextSibling: null } image }
                        when EmbeddedImage(image, inline: false) is { } figure:
                        output.Add(new BlockUIContainer(figure) { Margin = new(0, 3, 0, 17) });
                        break;
                    case ParagraphBlock paragraph:
                        var text = new Paragraph { Margin = new(0, 0, 0, 13) };
                        AppendInlines(text.Inlines, paragraph.Inline);
                        output.Add(text);
                        break;
                    case CodeBlock code:
                        output.Add(Code(code));
                        break;
                    case QuoteBlock quote:
                        var section = new Section
                        {
                            Padding = new(13, 8, 8, 1), BorderThickness = new(3, 0, 0, 0), Margin = new(0, 2, 0, 14),
                        };
                        section.SetResourceReference(Section.BorderBrushProperty, "WorkbenchAccent");
                        section.SetResourceReference(TextElement.BackgroundProperty, "WorkbenchSidebar");
                        AppendBlocks(section.Blocks, quote);
                        output.Add(section);
                        break;
                    case ListBlock list:
                        output.Add(RenderList(list));
                        break;
                    case Markdig.Extensions.Tables.Table table:
                        output.Add(RenderTable(table));
                        break;
                    case ThematicBreakBlock:
                        var line = new Paragraph { BorderThickness = new(0, 1, 0, 0), Margin = new(0, 7, 0, 12), LineHeight = 3 };
                        line.SetResourceReference(Paragraph.BorderBrushProperty, "WorkbenchEdge");
                        output.Add(line);
                        break;
                }
            }
        }

        private Paragraph Heading(HeadingBlock heading)
        {
            var size = heading.Level switch { 1 => 30, 2 => 23, 3 => 18, 4 => 16, _ => 15 };
            var title = new Paragraph
            {
                FontSize = size, LineHeight = size * 1.36, FontWeight = FontWeights.SemiBold, KeepWithNext = true,
                Margin = new(0, heading.Level == 1 ? 3 : 16, 0, 9),
            };
            if (heading.Level == 2)
            {
                title.Padding = new(0, 0, 0, 5);
                title.BorderThickness = new(0, 0, 0, 1);
                title.SetResourceReference(Paragraph.BorderBrushProperty, "WorkbenchEdge");
            }
            AppendInlines(title.Inlines, heading.Inline);
            if (heading.GetAttributes().Id is { Length: > 0 } id)
                _headings.TryAdd(id, title);
            return title;
        }

        private static Section Code(CodeBlock code)
        {
            var section = new Section { BorderThickness = new(1), Padding = new(12, 7, 12, 7), Margin = new(0, 1, 0, 15) };
            section.SetResourceReference(Section.BorderBrushProperty, "WorkbenchEdge");
            section.SetResourceReference(TextElement.BackgroundProperty, "WorkbenchSidebar");
            if (code is FencedCodeBlock { Info: { Length: > 0 } language })
            {
                var label = new Paragraph(new Run(language))
                {
                    FontSize = 11, LineHeight = 16, Margin = new(0, 0, 0, 5), KeepWithNext = true,
                };
                label.SetResourceReference(TextElement.ForegroundProperty, "WorkbenchMuted");
                section.Blocks.Add(label);
            }
            section.Blocks.Add(new Paragraph(new Run(code.Lines.ToString()))
            {
                FontFamily = Monospace, FontSize = 13, LineHeight = 21, Margin = new(0),
            });
            return section;
        }

        private List RenderList(ListBlock source)
        {
            var items = source.OfType<ListItemBlock>().ToArray();
            var tasksOnly = items.Length > 0 && items.All(item =>
                item.FirstOrDefault() is ParagraphBlock { Inline.FirstChild: TaskList });
            var list = new List
            {
                MarkerStyle = tasksOnly ? TextMarkerStyle.None : source.IsOrdered ? TextMarkerStyle.Decimal : TextMarkerStyle.Disc,
                Padding = new(tasksOnly ? 12 : 23, 0, 0, 0), Margin = new(0, 0, 0, 10),
            };
            if (source.IsOrdered && int.TryParse(source.OrderedStart, out var start) && start > 0)
                list.StartIndex = start;
            foreach (var item in items)
            {
                var row = new ListItem();
                AppendBlocks(row.Blocks, item);
                foreach (var text in row.Blocks.OfType<Paragraph>())
                    text.Margin = new(0, 0, 0, 3);
                list.ListItems.Add(row);
            }
            return list;
        }

        private System.Windows.Documents.Table RenderTable(Markdig.Extensions.Tables.Table source)
        {
            var table = new System.Windows.Documents.Table { CellSpacing = 0, Margin = new(0, 1, 0, 15) };
            var group = new TableRowGroup();
            table.RowGroups.Add(group);
            for (var index = 0; index < source.ColumnDefinitions.Count; index++)
                table.Columns.Add(new TableColumn { Width = new GridLength(1, GridUnitType.Star) });
            foreach (var sourceRow in source.OfType<Markdig.Extensions.Tables.TableRow>())
            {
                var row = new System.Windows.Documents.TableRow();
                foreach (var sourceCell in sourceRow.OfType<Markdig.Extensions.Tables.TableCell>())
                {
                    var column = row.Cells.Count;
                    var cell = new System.Windows.Documents.TableCell
                    {
                        Padding = new(9, 5, 9, 5), BorderThickness = new(column == 0 ? 1 : 0, group.Rows.Count == 0 ? 1 : 0, 1, 1),
                    };
                    cell.SetResourceReference(System.Windows.Documents.TableCell.BorderBrushProperty, "WorkbenchEdge");
                    if (sourceRow.IsHeader)
                    {
                        cell.FontWeight = FontWeights.SemiBold;
                        cell.SetResourceReference(TextElement.BackgroundProperty, "WorkbenchTint");
                    }
                    else if (group.Rows.Count % 2 == 0)
                        cell.SetResourceReference(TextElement.BackgroundProperty, "WorkbenchSidebar");
                    AppendBlocks(cell.Blocks, sourceCell);
                    foreach (var paragraph in cell.Blocks.OfType<Paragraph>())
                    {
                        paragraph.Margin = new(0);
                        if (column < source.ColumnDefinitions.Count)
                        {
                            paragraph.TextAlignment = source.ColumnDefinitions[column].Alignment switch
                            {
                                TableColumnAlign.Center => TextAlignment.Center,
                                TableColumnAlign.Right => TextAlignment.Right,
                                _ => TextAlignment.Left,
                            };
                        }
                    }
                    row.Cells.Add(cell);
                }
                group.Rows.Add(row);
            }
            return table;
        }

        private void AppendInlines(InlineCollection output, ContainerInline? source)
        {
            if (source is null)
                return;
            foreach (var child in source)
            {
                switch (child)
                {
                    case LiteralInline literal:
                        output.Add(new Run(literal.Content.ToString()));
                        break;
                    case CodeInline code:
                        var token = new Run(code.Content) { FontFamily = Monospace, FontSize = 13 };
                        token.SetResourceReference(TextElement.BackgroundProperty, "WorkbenchTint");
                        output.Add(token);
                        break;
                    case LineBreakInline { IsHard: true }: output.Add(new LineBreak()); break;
                    case LineBreakInline: output.Add(new Run(" ")); break;
                    case TaskList task:
                        var check = new Run(task.Checked ? "☑ " : "☐ ");
                        check.SetResourceReference(TextElement.ForegroundProperty, task.Checked ? "WorkbenchGitAccent" : "WorkbenchMuted");
                        output.Add(check);
                        break;
                    case EmphasisInline emphasis:
                        Span span = emphasis.DelimiterChar == '~' ? new Span { TextDecorations = TextDecorations.Strikethrough }
                            : emphasis.DelimiterCount >= 2 ? new Bold() : new Italic();
                        AppendInlines(span.Inlines, emphasis);
                        output.Add(span);
                        break;
                    case LinkInline { IsImage: true } image when EmbeddedImage(image, inline: true) is { } picture:
                        output.Add(new InlineUIContainer(picture));
                        break;
                    case LinkInline link:
                        AddLink(output, link);
                        break;
                    case AutolinkInline link:
                        output.Add(new Run(link.Url));
                        break;
                    case HtmlEntityInline entity:
                        output.Add(new Run(entity.Transcoded.ToString()));
                        break;
                    case ContainerInline nested:
                        AppendInlines(output, nested);
                        break;
                }
            }
        }

        private Image? EmbeddedImage(LinkInline link, bool inline)
        {
            if (images is null || LinkUrl(link) is not { } url || !images.TryGetValue(url, out var bitmap))
                return null;
            var alt = string.Concat(link.Descendants<LiteralInline>().Select(part => part.Content.ToString()));
            var image = new Image
            {
                Source = bitmap, Stretch = Stretch.Uniform, StretchDirection = StretchDirection.DownOnly,
                HorizontalAlignment = HorizontalAlignment.Center, MaxHeight = inline ? 52 : 440, ToolTip = alt,
            };
            if (inline)
                image.MaxWidth = 160;
            AutomationProperties.SetName(image, alt.Length == 0 ? "文档图片" : alt);
            return image;
        }

        private void AddLink(InlineCollection output, LinkInline link)
        {
            var url = LinkUrl(link);
            Action? follow = null;
            if (url is { Length: > 1 } && url[0] == '#')
            {
                var anchor = Uri.UnescapeDataString(url[1..]);
                follow = () =>
                {
                    if (_headings.TryGetValue(anchor, out var heading))
                        heading.BringIntoView();
                };
            }
            else if (url is not null && resolve(url) is { } local)
                follow = () => open(local);

            Span text;
            if (follow is not null)
            {
                var clickable = new Hyperlink { ToolTip = url };
                clickable.SetResourceReference(TextElement.ForegroundProperty, "WorkbenchAccent");
                clickable.Click += (_, _) => follow();
                text = clickable;
            }
            else
                text = new Span();

            AppendInlines(text.Inlines, link);
            if (text.Inlines.Count == 0 && url is not null)
                text.Inlines.Add(new Run(url));
            output.Add(text);
        }
    }
}
