using System.IO;
using System.Xml;
using ICSharpCode.AvalonEdit.Highlighting;
using ICSharpCode.AvalonEdit.Highlighting.Xshd;

namespace SciDev.Desktop;

internal static class PaperHtmlSyntax
{
    internal static IHighlightingDefinition Definition { get; } = Load();

    private static IHighlightingDefinition Load()
    {
        // The colors and lexical categories follow the Python Paper editor.
        using var text = new StringReader("""
            <SyntaxDefinition name="HTML Paper" xmlns="http://icsharpcode.net/sharpdevelop/syntaxdefinition/2008">
              <Color name="Keyword" foreground="#7C3AED" fontWeight="bold"/>
              <Color name="Tag" foreground="#0369A1"/>
              <Color name="Value" foreground="#0F766E"/>
              <Color name="Comment" foreground="#697386"/>
              <Color name="String" foreground="#B45309"/>
              <RuleSet name="Strings">
                <Span color="String" begin="&quot;" end="&quot;"/>
                <Span color="String" begin="'" end="'"/>
              </RuleSet>
              <RuleSet name="Attributes">
                <Import ruleSet="Strings"/>
                <Rule color="Keyword">[\w:-]+(?=\s*=)</Rule>
                <Rule color="Tag">[=/]</Rule>
              </RuleSet>
              <RuleSet name="Code">
                <Span color="Comment" begin="/\*" end="\*/" multiline="true"/>
                <Import ruleSet="Strings"/>
                <Rule color="Value">\b\d+(?:\.\d+)?(?:px|rem|em|vh|vw|ms|s|%)?\b</Rule>
              </RuleSet>
              <RuleSet name="Css">
                <Import ruleSet="Code"/>
                <Rule color="Tag">[#.][\w-]+|[$\w-]+(?=\s*:)</Rule>
                <Rule color="Value">--[\w-]+</Rule>
              </RuleSet>
              <RuleSet name="Script">
                <Import ruleSet="Code"/>
                <Span color="String" begin="\x60" end="\x60"/>
                <Rule color="Comment">//[^\r\n]*</Rule>
                <Rule color="Tag">\b(true|false|null|undefined|this)\b</Rule>
                <Rule color="Keyword">\b(const|let|var|function|return|if|else|for|while|class|extends|import|export|from|async|await|new|try|catch|finally|throw|switch|case|break|continue|typeof|instanceof|delete|void|yield|interface|type|public|private|readonly)\b</Rule>
              </RuleSet>
              <RuleSet>
                <Span color="Comment" begin="&lt;!--" end="--&gt;" multiline="true"/>
                <Span ruleSet="Css" multiline="true">
                  <Begin color="Tag">(?i)&lt;style\b[^&gt;]*&gt;</Begin>
                  <End color="Tag">(?i)&lt;/style\s*&gt;</End>
                </Span>
                <Span ruleSet="Script" multiline="true">
                  <Begin color="Tag">(?i)&lt;script\b[^&gt;]*&gt;</Begin>
                  <End color="Tag">(?i)&lt;/script\s*&gt;</End>
                </Span>
                <Span ruleSet="Attributes" multiline="true">
                  <Begin color="Tag">&lt;/?[a-zA-Z][\w:-]*</Begin>
                  <End color="Tag">&gt;</End>
                </Span>
              </RuleSet>
            </SyntaxDefinition>
            """);
        using var xml = XmlReader.Create(text);
        return HighlightingLoader.Load(xml, HighlightingManager.Instance);
    }
}
